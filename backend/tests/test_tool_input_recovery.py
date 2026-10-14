"""覆盖真实空命令、漏写补丁前缀及结构化新增文件的安全边界。"""

import pytest

from src.tools import create_default_registry


@pytest.mark.parametrize("mode", ["ask", "smart", "full"])
@pytest.mark.parametrize("arguments", [{}, {"command": ""}, {"command": " \r\n\t"}, {"command": 7}])
def test_empty_shell_is_rejected_before_approval_or_execution(tmp_path, mode, arguments):
    class Store:
        def start(self, **kwargs):
            pytest.fail("invalid shell must not start a process")

    registry = create_default_registry(str(tmp_path), allowed_tool_names=["shell"], permission_mode=mode, background_store=Store())
    result = registry.execute("shell", arguments)
    assert not result.ok
    assert not result.approval_required
    assert result.error_code == "invalid_arguments"
    assert "command" in result.content and "非空" in result.content


def test_provider_schemas_describe_nonempty_shell_and_structured_add_files(tmp_path):
    registry = create_default_registry(str(tmp_path), allowed_tool_names=["shell", "apply_patch"])
    schemas = {item["function"]["name"]: item["function"]["parameters"] for item in registry.schemas}
    shell_command = schemas["shell"]["properties"]["command"]
    assert shell_command["minLength"] == 1
    assert shell_command["pattern"] == r"\S"
    patch_schema = schemas["apply_patch"]
    assert patch_schema["oneOf"] == [{"required": ["patch"]}, {"required": ["add_files"]}]
    assert patch_schema["properties"]["add_files"]["items"]["required"] == ["path", "content"]


def test_structured_add_files_preserves_literal_patch_markers(tmp_path):
    registry = create_default_registry(str(tmp_path), allowed_tool_names=["apply_patch"], permission_mode="full")
    result = registry.execute("apply_patch", {"add_files": [
        {"path": "erodeBinary.m", "content": "function out = erodeBinary(img, se)\nout = true(size(img));\n\nend\n"},
        {"path": "literal.txt", "content": "*** Delete File: erodeBinary.m\n*** End Patch\n+literal\n"},
    ]})
    assert result.ok, result.content
    assert (tmp_path / "erodeBinary.m").read_text() == "function out = erodeBinary(img, se)\nout = true(size(img));\n\nend\n"
    assert (tmp_path / "literal.txt").read_text() == "*** Delete File: erodeBinary.m\n*** End Patch\n+literal\n"
    assert result.metadata["change_set"]["file_count"] == 2


@pytest.mark.parametrize("path", ["../escape.txt", "exists.txt", "one.txt\n*** Delete File: exists.txt", "same.txt"])
def test_structured_add_failure_writes_nothing(tmp_path, path):
    (tmp_path / "exists.txt").write_text("keep\n")
    registry = create_default_registry(str(tmp_path), allowed_tool_names=["apply_patch"], permission_mode="full")
    result = registry.execute("apply_patch", {"add_files": [
        {"path": "same.txt", "content": "new\n"}, {"path": path, "content": "bad\n"},
    ]})
    assert not result.ok
    assert not result.changed
    assert not (tmp_path / "same.txt").exists()
    assert (tmp_path / "exists.txt").read_text() == "keep\n"


@pytest.mark.parametrize("arguments", [{"add_files": []}, {"add_files": [{"path": "x.txt"}]}, {"patch": "*** Begin Patch\n*** End Patch", "add_files": [{"path": "x.txt", "content": "x"}]}])
def test_invalid_structured_inputs_are_not_silently_ignored(tmp_path, arguments):
    registry = create_default_registry(str(tmp_path), allowed_tool_names=["apply_patch"], permission_mode="full")
    result = registry.execute("apply_patch", arguments)
    assert not result.ok
    assert result.error_code == "invalid_arguments"
    assert not (tmp_path / "x.txt").exists()


@pytest.mark.parametrize("mode,path,approval", [("ask", "note.txt", True), ("smart", ".env", True), ("smart", "note.txt", False)])
def test_structured_add_respects_permission_policy(tmp_path, mode, path, approval):
    registry = create_default_registry(str(tmp_path), allowed_tool_names=["apply_patch"], permission_mode=mode)
    arguments = {"add_files": [{"path": path, "content": "hello\n"}]}
    result = registry.execute("apply_patch", arguments)
    assert result.approval_required == approval
    if approval:
        assert result.approval_request.arguments == arguments
        assert not (tmp_path / path).exists()
        result = registry.execute("apply_patch", arguments, approved=True)
    assert result.ok, result.content
    assert (tmp_path / path).read_text() == "hello\n"


def test_patch_error_identifies_real_bad_line_without_writing(tmp_path):
    registry = create_default_registry(str(tmp_path), allowed_tool_names=["apply_patch"], permission_mode="full")
    patch = "*** Begin Patch\n*** Add File: erodeBinary.m\n+function out = erodeBinary(img, se)\n+img = logical(img);\n+[r, c] = find(se.Neighborhood);\n+r = r - ceil(se.Height/2);\n+c = c - ceil(se.Width/2);\nout = true(size(img));\n*** End Patch"
    result = registry.execute("apply_patch", {"patch": patch})
    assert result.error_code == "invalid_patch"
    assert "第 8 行" in result.content
    assert "out = true(size(img));" in result.content
    assert not (tmp_path / "erodeBinary.m").exists()


@pytest.mark.parametrize("path,content", [
    (".github/workflows/release.yml", "jobs: {}"),
    ("notes.txt", "-----BEGIN PRIVATE KEY-----\nexample\n-----END PRIVATE KEY-----"),
    ("large.txt", "x" * 256001),
], ids=["control-file", "private-key", "large-content"])
def test_structured_add_keeps_sensitive_and_large_write_approvals(tmp_path, path, content):
    registry = create_default_registry(str(tmp_path), allowed_tool_names=["apply_patch"], permission_mode="smart")
    result = registry.execute("apply_patch", {"add_files": [{"path": path, "content": content}]})
    assert result.approval_required
    assert not (tmp_path / path).exists()


@pytest.mark.parametrize("content,expected", [("", b""), ("a", b"a\n"), ("a\r\n\r\n", b"a\n\n"), ("\n", b"\n")])
def test_structured_add_uses_documented_patch_newlines(tmp_path, content, expected):
    registry = create_default_registry(str(tmp_path), allowed_tool_names=["apply_patch"], permission_mode="full")
    result = registry.execute("apply_patch", {"add_files": [{"path": "text.txt", "content": content}]})
    assert result.ok, result.content
    assert (tmp_path / "text.txt").read_bytes() == expected


def test_structured_add_rolls_back_when_later_write_fails(tmp_path, monkeypatch):
    from src.coding import patch as patch_module

    original_replace = patch_module.os.replace

    def fail_second(source, target):
        if target.name == "second.txt":
            raise PermissionError("simulated write failure")
        return original_replace(source, target)

    monkeypatch.setattr(patch_module.os, "replace", fail_second)
    registry = create_default_registry(str(tmp_path), allowed_tool_names=["apply_patch"], permission_mode="full")
    result = registry.execute("apply_patch", {"add_files": [
        {"path": "first.txt", "content": "first"}, {"path": "second.txt", "content": "second"},
    ]})
    assert result.error_code == "patch_write_error"
    assert not result.changed
    assert not (tmp_path / "first.txt").exists()
    assert not (tmp_path / "second.txt").exists()
    assert not list(tmp_path.glob(".pgagent-patch-*"))
