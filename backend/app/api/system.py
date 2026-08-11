"""Local-machine helpers that cannot be implemented in a browser sandbox."""

from __future__ import annotations

import ipaddress
import json
import platform
import subprocess
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel


router = APIRouter(prefix="/api/system", tags=["system"])

_FOLDER_DIALOG_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
Add-Type -AssemblyName System.Windows.Forms
$dialog = [System.Windows.Forms.FolderBrowserDialog]::new()
$dialog.Description = 'Select a PGAgent workspace folder'
$dialog.ShowNewFolderButton = $true
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    @{ path = $dialog.SelectedPath; cancelled = $false } | ConvertTo-Json -Compress
} else {
    @{ path = $null; cancelled = $true } | ConvertTo-Json -Compress
}
"""


class FolderSelectionRead(BaseModel):
    path: str | None
    cancelled: bool


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _run_folder_dialog() -> dict[str, Any]:
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-STA", "-Command", _FOLDER_DIALOG_SCRIPT],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=300,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or "Windows folder picker failed"
        raise RuntimeError(message)
    lines = [line.strip().lstrip("\ufeff") for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("Windows folder picker returned no result")
    result = json.loads(lines[-1])
    if not isinstance(result, dict) or not isinstance(result.get("cancelled"), bool):
        raise RuntimeError("Windows folder picker returned an invalid result")
    path = result.get("path")
    if path is not None and not isinstance(path, str):
        raise RuntimeError("Windows folder picker returned an invalid path")
    return {"path": path, "cancelled": result["cancelled"]}


@router.post("/select-folder", response_model=FolderSelectionRead)
def select_folder(request: Request) -> FolderSelectionRead:
    client_host = request.client.host if request.client is not None else ""
    if not _is_loopback(client_host):
        raise HTTPException(status_code=403, detail="Folder selection is available only from this machine")
    if platform.system() != "Windows":
        raise HTTPException(status_code=501, detail="Native folder selection is available only on Windows")
    try:
        return FolderSelectionRead(**_run_folder_dialog())
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="Folder selection timed out") from exc
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
