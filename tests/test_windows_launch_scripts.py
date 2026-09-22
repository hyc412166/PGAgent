from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_windows_powershell_can_parse_launch_scripts() -> None:
    """Windows PowerShell 5.1 必须能直接解析双击入口调用的两个脚本。"""

    script_paths = [
        PROJECT_ROOT / "scripts" / "setup.ps1",
        PROJECT_ROOT / "scripts" / "start.ps1",
        PROJECT_ROOT / "scripts" / "update-ripgrep.ps1",
    ]
    for script_path in script_paths:
        escaped_path = str(script_path).replace("'", "''")
        parser_command = (
            "$errors = $null; $tokens = $null; "
            "$null = [System.Management.Automation.Language.Parser]::ParseFile("
            f"'{escaped_path}', [ref]$tokens, [ref]$errors); "
            "@($errors) | Select-Object Message, ErrorId | ConvertTo-Json -Compress"
        )
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                parser_command,
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        parser_errors = json.loads(result.stdout) if result.stdout.strip() else []
        assert result.returncode == 0
        assert parser_errors == [], f"{script_path.name}: {parser_errors}"


def test_windows_launch_scripts_bootstrap_the_independent_node_runtime() -> None:
    """启动脚本不能依赖旧进程 PATH，必须固定使用用户维护的 Node/npm。"""

    expected_node_root = r"$env:USERPROFILE\Tools\nodejs\22.22.2"
    for script_name in ("setup.ps1", "start.ps1"):
        script = (PROJECT_ROOT / "scripts" / script_name).read_text(encoding="utf-8-sig")
        assert expected_node_root in script
        assert "npm.cmd" in script
        assert "pnpm" not in script


def test_start_script_reuses_a_healthy_pgagent_before_starting_uvicorn() -> None:
    """重复双击入口时必须在 Uvicorn 启动恢复之前复用现有服务。"""

    script = (PROJECT_ROOT / "scripts" / "start.ps1").read_text(encoding="utf-8-sig")
    health_check = 'Invoke-WebRequest -UseBasicParsing -Uri "$Url/api/health"'
    uvicorn_start = "-m uvicorn src.main:app"

    assert health_check in script
    assert "$existingHealth.name -eq 'PGAgent'" in script
    assert script.index(health_check) < script.index(uvicorn_start)


def test_pgagent_bundled_ripgrep_is_usable_and_precedes_inherited_path() -> None:
    """PGAgent 必须运行自己携带的 rg，而不是偶然继承 Codex 或系统版本。"""

    bundled_directory = PROJECT_ROOT / "vendor" / "ripgrep" / "windows-x64"
    bundled_rg = bundled_directory / "rg.exe"
    assert bundled_rg.is_file()
    assert (PROJECT_ROOT / "vendor" / "ripgrep" / "LICENSE-MIT").is_file()
    assert (PROJECT_ROOT / "vendor" / "ripgrep" / "UNLICENSE").is_file()

    result = subprocess.run(
        [str(bundled_rg), "--version"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0
    assert result.stdout.startswith("ripgrep ")

    expected_directory = r"vendor\ripgrep\windows-x64"
    for script_name in ("setup.ps1", "start.ps1"):
        script = (PROJECT_ROOT / "scripts" / script_name).read_text(encoding="utf-8-sig")
        assert expected_directory in script
        path_assignment = '$env:Path = "$RipgrepDirectory;$env:Path"'
        assert path_assignment in script

    # Windows 的 PATH 不区分大小写；首项必须是项目自带目录。
    process_path = f"{bundled_directory}{os.pathsep}{os.environ.get('PATH', '')}"
    assert Path(process_path.split(os.pathsep, 1)[0]).resolve() == bundled_directory.resolve()
