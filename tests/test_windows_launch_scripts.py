from __future__ import annotations

import json
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_windows_powershell_can_parse_launch_scripts() -> None:
    """Windows PowerShell 5.1 必须能直接解析双击入口调用的两个脚本。"""

    script_paths = [
        PROJECT_ROOT / "scripts" / "setup.ps1",
        PROJECT_ROOT / "scripts" / "start.ps1",
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
