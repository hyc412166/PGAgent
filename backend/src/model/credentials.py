"""OS credential-store adapter for model provider API keys."""

from __future__ import annotations

import keyring
from keyring.errors import KeyringError, PasswordDeleteError


SERVICE_NAME = "PGAgent"


class SecretStoreError(RuntimeError):
    """Raised when the operating-system credential store is unavailable."""


def secret_ref_for_connection(connection_id: str) -> str:
    return f"model-connection:{connection_id}"


def save_api_key(secret_ref: str, api_key: str) -> None:
    if not api_key.strip():
        raise ValueError("API key cannot be empty")
    try:
        keyring.set_password(SERVICE_NAME, secret_ref, api_key)
    except KeyringError as exc:
        raise SecretStoreError("Unable to save API key in the operating-system credential store") from exc


def get_api_key(secret_ref: str) -> str | None:
    try:
        return keyring.get_password(SERVICE_NAME, secret_ref)
    except KeyringError as exc:
        raise SecretStoreError("Unable to read API key from the operating-system credential store") from exc


def delete_api_key(secret_ref: str) -> None:
    try:
        keyring.delete_password(SERVICE_NAME, secret_ref)
    except PasswordDeleteError:
        return
    except KeyringError as exc:
        raise SecretStoreError("Unable to delete API key from the operating-system credential store") from exc
