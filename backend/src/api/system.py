"""Local-machine helpers that cannot be implemented in a browser sandbox."""
# 文件职责：负责HTTP 接口、数据契约与依赖装配中的 system 子模块。
# 逻辑关系：上层通过 api/system.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

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


# 变量说明：router 表示当前步骤使用的 router 值。
router = APIRouter(prefix="/api/system", tags=["system"])



# 类职责：定义 FolderSelectionRead 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class FolderSelectionRead(BaseModel):
    # 变量说明：path 表示当前文件或目录路径。
    path: str | None
    # 变量说明：cancelled 表示当前步骤使用的 cancelled 值。
    cancelled: bool


# 类职责：定义 FolderSelectionRequest 的跨层数据契约。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class FolderSelectionRequest(BaseModel):
    # 变量说明：title 表示当前步骤使用的 title 值。
    title: str = Field(default="Select a Folder", min_length=1, max_length=80)


# 类职责：定义 PersonalizationUpdate 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class PersonalizationUpdate(BaseModel):
    # 变量说明：custom_instructions 表示当前流程使用的 custom_instructions 集合。
    custom_instructions: str = Field(default="", max_length=MAX_PERSONAL_INSTRUCTION_CHARS)


# 类职责：定义 PersonalizationRead 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class PersonalizationRead(BaseModel):
    # 变量说明：custom_instructions 表示当前流程使用的 custom_instructions 集合。
    custom_instructions: str
    # 变量说明：effective_instructions 表示当前流程使用的 effective_instructions 集合。
    effective_instructions: str
    # 变量说明：agents_path 表示agents_path 对应的文件系统位置。
    agents_path: str
    # 变量说明：effective_path 表示effective_path 对应的文件系统位置。
    effective_path: str
    # 变量说明：override_active 表示当前步骤使用的 override_active 值。
    override_active: bool
    # 变量说明：max_characters 表示当前流程使用的 max_characters 集合。
    max_characters: int = MAX_PERSONAL_INSTRUCTION_CHARS


# 函数职责：完成 is_loopback 对应的业务处理。
# 参数关系：host 表示当前步骤使用的 host 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


# 函数职责：完成 require_loopback 对应的业务处理。
# 参数关系：request 表示调用方传入的请求数据。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _require_loopback(request: Request) -> None:
    # 变量说明：client_host 表示当前步骤使用的 client_host 值。
    client_host = request.client.host if request.client is not None else ""
    if not _is_loopback(client_host):
        raise HTTPException(status_code=403, detail="Personalization is only available from this machine")


# 函数职责：完成 personalization_payload 对应的业务处理。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _personalization_payload() -> PersonalizationRead:
    # 变量说明：effective 表示当前步骤使用的 effective 值；effective_path 表示effective_path 对应的文件系统位置；override_active 表示当前步骤使用的 override_active 值。
    effective, effective_path, override_active = effective_personal_instructions()
    return PersonalizationRead(
        custom_instructions=read_personal_instructions(),
        effective_instructions=effective,
        agents_path=str(personal_agents_path().resolve()),
        effective_path=str(effective_path.resolve()),
        override_active=override_active,
    )


# 函数职责：读取 personalization 对应的数据或流程。
# 参数关系：request 表示调用方传入的请求数据。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.get("/personalization", response_model=PersonalizationRead)
def get_personalization(request: Request) -> PersonalizationRead:
    _require_loopback(request)
    return _personalization_payload()


# 函数职责：更新 personalization 对应的数据或流程。
# 参数关系：payload 表示跨层传递的数据载荷；request 表示调用方传入的请求数据。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.put("/personalization", response_model=PersonalizationRead)
def update_personalization(payload: PersonalizationUpdate, request: Request) -> PersonalizationRead:
    _require_loopback(request)
    try:
        write_personal_instructions(payload.custom_instructions)
    except OSError as exc:
        raise HTTPException(status_code=503, detail=f"Could not save AGENTS.md: {exc}") from exc
    return _personalization_payload()



# 函数职责：完成 select_folder 对应的业务处理。
# 参数关系：request 表示调用方传入的请求数据；payload 表示跨层传递的数据载荷。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.post("/select-folder", response_model=FolderSelectionRead)
def select_folder(request: Request, payload: FolderSelectionRequest | None = None) -> FolderSelectionRead:
    # 变量说明：client_host 表示当前步骤使用的 client_host 值。
    client_host = request.client.host if request.client is not None else ""
    if not _is_loopback(client_host):
        raise HTTPException(status_code=403, detail="Folder selection is available only from this machine")
    if platform.system() != "Windows":
        raise HTTPException(status_code=501, detail="Native folder selection is available only on Windows")
    try:
        # 变量说明：title 表示当前步骤使用的 title 值。
        title = payload.title if payload is not None else "Select a Folder"
        # 变量说明：path 表示当前文件或目录路径。
        path = show_folder_picker(title)
        return FolderSelectionRead(path=path, cancelled=path is None)
    except OSError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
