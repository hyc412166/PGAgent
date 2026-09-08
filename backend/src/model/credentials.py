"""OS credential-store adapter for model provider API keys."""
# 文件职责：负责模型连接、请求、协议转换与流式响应中的 credentials 子模块。
# 逻辑关系：上层通过 model/credentials.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

import keyring
from keyring.errors import KeyringError, PasswordDeleteError


# 变量说明：SERVICE_NAME 表示当前步骤使用的 SERVICE_NAME 值。
SERVICE_NAME = "PGAgent"


# 类职责：表示 SecretStoreError 场景的领域异常。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class SecretStoreError(RuntimeError):
    """Raised when the operating-system credential store is unavailable."""


# 函数职责：完成 secret_ref_for_connection 对应的业务处理。
# 参数关系：connection_id 表示connection 对象的唯一标识。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def secret_ref_for_connection(connection_id: str) -> str:
    return f"model-connection:{connection_id}"


# 函数职责：保存 api_key 对应的数据或流程。
# 参数关系：secret_ref 表示当前步骤使用的 secret_ref 值；api_key 表示当前步骤使用的 api_key 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def save_api_key(secret_ref: str, api_key: str) -> None:
    if not api_key.strip():
        raise ValueError("API key cannot be empty")
    try:
        keyring.set_password(SERVICE_NAME, secret_ref, api_key)
    except KeyringError as exc:
        raise SecretStoreError("Unable to save API key in the operating-system credential store") from exc


# 函数职责：读取 api_key 对应的数据或流程。
# 参数关系：secret_ref 表示当前步骤使用的 secret_ref 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def get_api_key(secret_ref: str) -> str | None:
    try:
        return keyring.get_password(SERVICE_NAME, secret_ref)
    except KeyringError as exc:
        raise SecretStoreError("Unable to read API key from the operating-system credential store") from exc


# 函数职责：删除 api_key 对应的数据或流程。
# 参数关系：secret_ref 表示当前步骤使用的 secret_ref 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def delete_api_key(secret_ref: str) -> None:
    try:
        keyring.delete_password(SERVICE_NAME, secret_ref)
    except PasswordDeleteError:
        return
    except KeyringError as exc:
        raise SecretStoreError("Unable to delete API key from the operating-system credential store") from exc
