"""Local-machine helpers that cannot be implemented in a browser sandbox."""

from __future__ import annotations

import ipaddress
import platform

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from src.context.instructions import (
    MAX_PERSONAL_INSTRUCTION_CHARS,
    effective_personal_instructions,
    personal_agents_path,
    read_personal_instructions,
    write_personal_instructions,
)
from src.native.folder_picker import show_folder_picker


router = APIRouter(prefix="/api/system", tags=["system"])



class FolderSelectionRead(BaseModel):
    path: str | None
    cancelled: bool


class FolderSelectionRequest(BaseModel):
    title: str = Field(default="Select a Folder", min_length=1, max_length=80)


class PersonalizationUpdate(BaseModel):
    custom_instructions: str = Field(default="", max_length=MAX_PERSONAL_INSTRUCTION_CHARS)


class PersonalizationRead(BaseModel):
    custom_instructions: str
    effective_instructions: str
    agents_path: str
    effective_path: str
    override_active: bool
    max_characters: int = MAX_PERSONAL_INSTRUCTION_CHARS


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _require_loopback(request: Request) -> None:
    client_host = request.client.host if request.client is not None else ""
    if not _is_loopback(client_host):
        raise HTTPException(status_code=403, detail="Personalization is only available from this machine")


def _personalization_payload() -> PersonalizationRead:
    effective, effective_path, override_active = effective_personal_instructions()
    return PersonalizationRead(
        custom_instructions=read_personal_instructions(),
        effective_instructions=effective,
        agents_path=str(personal_agents_path().resolve()),
        effective_path=str(effective_path.resolve()),
        override_active=override_active,
    )


@router.get("/personalization", response_model=PersonalizationRead)
def get_personalization(request: Request) -> PersonalizationRead:
    _require_loopback(request)
    return _personalization_payload()


@router.put("/personalization", response_model=PersonalizationRead)
def update_personalization(payload: PersonalizationUpdate, request: Request) -> PersonalizationRead:
    _require_loopback(request)
    try:
        write_personal_instructions(payload.custom_instructions)
    except OSError as exc:
        raise HTTPException(status_code=503, detail=f"Could not save AGENTS.md: {exc}") from exc
    return _personalization_payload()



@router.post("/select-folder", response_model=FolderSelectionRead)
def select_folder(request: Request, payload: FolderSelectionRequest | None = None) -> FolderSelectionRead:
    client_host = request.client.host if request.client is not None else ""
    if not _is_loopback(client_host):
        raise HTTPException(status_code=403, detail="Folder selection is available only from this machine")
    if platform.system() != "Windows":
        raise HTTPException(status_code=501, detail="Native folder selection is available only on Windows")
    try:
        title = payload.title if payload is not None else "Select a Folder"
        path = show_folder_picker(title)
        return FolderSelectionRead(path=path, cancelled=path is None)
    except OSError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
