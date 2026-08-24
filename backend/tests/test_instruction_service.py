from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import instruction_service


@pytest.fixture()
def instruction_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(instruction_service, "settings", SimpleNamespace(data_dir=data_dir))
    return data_dir


def test_personalization_round_trips_through_global_agents_md(instruction_home: Path) -> None:
    path = instruction_service.write_personal_instructions("Prefer Chinese replies.\r\nRun tests.")

    assert path == instruction_home / "AGENTS.md"
    assert path.read_text(encoding="utf-8") == "Prefer Chinese replies.\nRun tests."
    assert instruction_service.read_personal_instructions() == "Prefer Chinese replies.\nRun tests."
    effective, effective_path, override_active = instruction_service.effective_personal_instructions()
    assert effective == "Prefer Chinese replies.\nRun tests."
    assert effective_path == path
    assert not override_active


def test_global_and_project_override_precedence_matches_codex(
    instruction_home: Path,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (instruction_home / "AGENTS.md").write_text("global base", encoding="utf-8")
    (instruction_home / "AGENTS.override.md").write_text("global override", encoding="utf-8")
    (workspace / "AGENTS.md").write_text("project base", encoding="utf-8")
    (workspace / "AGENTS.override.md").write_text("project override", encoding="utf-8")

    rendered, paths = instruction_service.load_instruction_chain(workspace)

    assert "global override" in rendered and "global base" not in rendered
    assert "project override" in rendered and "project base" not in rendered
    assert rendered.index("global override") < rendered.index("project override")
    assert paths == [
        str((instruction_home / "AGENTS.override.md").resolve()),
        str((workspace / "AGENTS.override.md").resolve()),
    ]


def test_empty_override_falls_back_and_workspace_rules_keep_instruction_order(
    instruction_home: Path,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (instruction_home / "AGENTS.md").write_text("personal rule", encoding="utf-8")
    (instruction_home / "AGENTS.override.md").write_text("", encoding="utf-8")
    (workspace / "AGENTS.md").write_text("project rule", encoding="utf-8")

    rendered, _paths = instruction_service.load_instruction_chain(workspace)
    rules = instruction_service.render_workspace_rules(str(workspace), rendered)

    assert rules.index("personal rule") < rules.index("project rule")
    assert rules.index("project rule") < rules.index("Only access the selected workspace")


def test_personal_instruction_limit_is_enforced(instruction_home: Path) -> None:
    with pytest.raises(ValueError, match="exceed"):
        instruction_service.write_personal_instructions(
            "x" * (instruction_service.MAX_PERSONAL_INSTRUCTION_CHARS + 1)
        )


def test_manually_created_instruction_files_are_bounded(
    instruction_home: Path,
    tmp_path: Path,
) -> None:
    override = instruction_home / "AGENTS.override.md"
    override.write_text(
        "x" * (instruction_service.MAX_PERSONAL_INSTRUCTION_CHARS + 100),
        encoding="utf-8",
    )
    effective, _path, active = instruction_service.effective_personal_instructions()

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_bytes(
        b"y" * (instruction_service.MAX_PROJECT_INSTRUCTION_BYTES + 100)
    )
    sources = instruction_service.discover_instruction_sources(workspace)

    assert active is True
    assert len(effective) == instruction_service.MAX_PERSONAL_INSTRUCTION_CHARS
    project = next(source for source in sources if source.scope == "project")
    assert len(project.content.encode("utf-8")) == instruction_service.MAX_PROJECT_INSTRUCTION_BYTES


def test_project_instruction_symlink_cannot_escape_workspace(
    instruction_home: Path,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside-agents.md"
    outside.write_text("external secret", encoding="utf-8")
    try:
        (workspace / "AGENTS.md").symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    rendered, paths = instruction_service.load_instruction_chain(workspace)

    assert "external secret" not in rendered
    assert str(outside.resolve()) not in paths


def test_resolved_file_outside_boundary_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside-agents.md"
    outside.write_text("external secret", encoding="utf-8")

    assert instruction_service._resolved_file_within(outside, workspace) is None
