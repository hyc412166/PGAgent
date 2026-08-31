"""CRUD and capability discovery for OpenAI-compatible model endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.persistence.database import ModelConnection, get_db, new_id
from src.api.schemas import (
    ConnectionTestResult,
    ModelConnectionCreate,
    ModelConnectionRead,
    ModelConnectionUpdate,
)
from src.model.credentials import (
    SecretStoreError,
    delete_api_key,
    get_api_key,
    save_api_key,
    secret_ref_for_connection,
)


router = APIRouter(prefix="/api/connections", tags=["model connections"])
REQUEST_TIMEOUT_SECONDS = 12.0


def _models_url(base_url: str) -> str:
    return f"{base_url.strip().rstrip('/')}/models"


def _protocol_url(base_url: str, protocol: str) -> str:
    endpoint = "responses" if protocol == "responses" else "chat/completions"
    return f"{base_url.strip().rstrip('/')}/{endpoint}"


def probe_protocol(
    base_url: str,
    api_key: str,
    protocol: str,
    model_id: str | None,
    custom_headers: dict[str, str] | None = None,
) -> ConnectionTestResult:
    """Verify the selected wire endpoint with the smallest practical model request."""

    if not model_id:
        return ConnectionTestResult(
            success=False, category="provider_error",
            message="检测模型协议前需要先选择模型 ID", retryable=False,
        )
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    headers.update(custom_headers or {})
    if protocol == "responses":
        payload: dict[str, Any] = {
            "model": model_id,
            "input": "ping",
            "max_output_tokens": 16,
            "store": False,
            "stream": True,
        }
    else:
        payload = {"model": model_id, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            if protocol == "responses":
                # 运行时始终消费 Responses 流；探测也必须走同一条路径，避免
                # “普通请求成功、首次流式调用才失败”的假阳性。
                with client.stream(
                    "POST", _protocol_url(base_url, protocol), headers=headers, json=payload
                ) as response:
                    response.read()
            else:
                response = client.post(_protocol_url(base_url, protocol), headers=headers, json=payload)
    except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
        return ConnectionTestResult(success=False, category="network_error", message=f"协议检测失败：{exc}", retryable=True)
    if response.status_code in {401, 403}:
        category = "invalid_credentials"
    elif response.status_code == 429:
        category = "rate_limited"
    elif response.status_code >= 500:
        category = "provider_error"
    elif response.status_code >= 400:
        category = "provider_error"
    else:
        return ConnectionTestResult(success=True, category="ok", message=f"已确认支持 {protocol}", retryable=False,
                                    http_status=response.status_code, models=[model_id])
    return ConnectionTestResult(success=False, category=category,
                                message=f"{protocol} 协议检测失败（HTTP {response.status_code}）",
                                retryable=response.status_code in {429} or response.status_code >= 500,
                                http_status=response.status_code)


def _parse_models(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    records = payload.get("data", payload.get("models", []))
    if not isinstance(records, list):
        return []
    found: list[str] = []
    for record in records:
        model_id: Any
        if isinstance(record, str):
            model_id = record
        elif isinstance(record, dict):
            model_id = record.get("id") or record.get("name")
        else:
            continue
        if isinstance(model_id, str) and model_id.strip():
            found.append(model_id.strip())
    return sorted(dict.fromkeys(found))


def discover_models(
    base_url: str,
    api_key: str,
    custom_headers: dict[str, str] | None = None,
) -> ConnectionTestResult:
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    headers.update(custom_headers or {})
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = client.get(_models_url(base_url), headers=headers)
    except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
        return ConnectionTestResult(
            success=False,
            category="network_error",
            message=f"Could not reach model endpoint: {exc}",
            retryable=True,
        )

    if response.status_code in (401, 403):
        return ConnectionTestResult(
            success=False,
            category="invalid_credentials",
            message=f"Provider rejected the API key (HTTP {response.status_code})",
            retryable=False,
            http_status=response.status_code,
        )
    if response.status_code == 429:
        return ConnectionTestResult(
            success=False,
            category="rate_limited",
            message="Provider rate limit reached (HTTP 429)",
            retryable=True,
            http_status=429,
        )
    if response.status_code >= 500:
        return ConnectionTestResult(
            success=False,
            category="provider_error",
            message=f"Provider is temporarily unavailable (HTTP {response.status_code})",
            retryable=True,
            http_status=response.status_code,
        )
    if response.status_code >= 400:
        return ConnectionTestResult(
            success=False,
            category="provider_error",
            message=f"Model discovery failed (HTTP {response.status_code})",
            retryable=False,
            http_status=response.status_code,
        )
    try:
        models = _parse_models(response.json())
    except ValueError:
        return ConnectionTestResult(
            success=False,
            category="provider_error",
            message="Provider returned invalid JSON from /models",
            retryable=False,
            http_status=response.status_code,
        )
    if not models:
        return ConnectionTestResult(
            success=False,
            category="provider_error",
            message="The /models response did not contain any model IDs",
            retryable=False,
            http_status=response.status_code,
        )
    return ConnectionTestResult(
        success=True,
        category="ok",
        message=f"Connected; discovered {len(models)} model(s)",
        retryable=False,
        http_status=response.status_code,
        models=models,
    )


def _require_connection(db: Session, connection_id: str) -> ModelConnection:
    connection = db.get(ModelConnection, connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="Model connection not found")
    return connection


def _save_or_503(secret_ref: str, api_key: str) -> None:
    try:
        save_api_key(secret_ref, api_key)
    except SecretStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("", response_model=list[ModelConnectionRead])
def list_connections(db: Session = Depends(get_db)) -> list[ModelConnection]:
    return list(db.scalars(select(ModelConnection).order_by(ModelConnection.created_at.desc())))


@router.post("", response_model=ModelConnectionRead, status_code=status.HTTP_201_CREATED)
def create_connection(payload: ModelConnectionCreate, db: Session = Depends(get_db)) -> ModelConnection:
    connection_id = new_id()
    secret_ref = secret_ref_for_connection(connection_id)
    result = discover_models(payload.base_url, payload.api_key, payload.custom_headers)
    # A missing/non-standard models endpoint can use manual IDs, but manual IDs
    # must never make a demonstrably invalid credential look usable.
    if not result.success and (result.category == "invalid_credentials" or not payload.manual_models):
        raise HTTPException(
            status_code=400,
            detail={
                "message": result.message,
                "category": result.category,
                "retryable": result.retryable,
                "http_status": result.http_status,
                "hint": "Supply manual_models when this provider does not expose /models",
            },
        )
    available_models = result.models or payload.manual_models
    default_model = payload.default_model or (available_models[0] if available_models else None)
    protocol_result = probe_protocol(
        payload.base_url, payload.api_key, payload.api_protocol, default_model, payload.custom_headers,
    )
    if not protocol_result.success:
        raise HTTPException(status_code=400, detail={
            "message": protocol_result.message, "category": protocol_result.category,
            "retryable": protocol_result.retryable, "http_status": protocol_result.http_status,
        })
    _save_or_503(secret_ref, payload.api_key)
    connection = ModelConnection(
        id=connection_id,
        name=payload.name,
        provider=payload.provider,
        api_protocol=payload.api_protocol,
        base_url=payload.base_url.strip().rstrip("/"),
        secret_ref=secret_ref,
        discovered_models=result.models,
        manual_models=payload.manual_models,
        default_model=default_model,
        thinking_level=payload.thinking_level,
        custom_headers=payload.custom_headers,
        status="connected" if result.success else "manual",
        last_error=None if result.success else result.message,
        last_checked_at=datetime.now(timezone.utc),
        capabilities={"model_discovery": result.success, payload.api_protocol: True},
        enabled=payload.enabled,
    )
    db.add(connection)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        try:
            delete_api_key(secret_ref)
        except SecretStoreError:
            pass
        raise HTTPException(status_code=409, detail="A connection with this name already exists") from exc
    db.refresh(connection)
    return connection


@router.get("/{connection_id}", response_model=ModelConnectionRead)
def get_connection(connection_id: str, db: Session = Depends(get_db)) -> ModelConnection:
    return _require_connection(db, connection_id)


@router.patch("/{connection_id}", response_model=ModelConnectionRead)
def update_connection(
    connection_id: str,
    payload: ModelConnectionUpdate,
    db: Session = Depends(get_db),
) -> ModelConnection:
    connection = _require_connection(db, connection_id)
    if payload.name is not None:
        duplicate = db.scalar(
            select(ModelConnection).where(
                ModelConnection.name == payload.name,
                ModelConnection.id != connection_id,
            )
        )
        if duplicate is not None:
            raise HTTPException(status_code=409, detail="A connection with this name already exists")
    updates = payload.model_dump(exclude_unset=True, exclude={"api_key"})
    for key, value in updates.items():
        if key == "base_url" and value:
            value = value.strip().rstrip("/")
        setattr(connection, key, value)
    should_discover = payload.api_key is not None or payload.base_url is not None or payload.custom_headers is not None
    should_probe = should_discover or payload.api_protocol is not None or payload.default_model is not None
    if should_discover:
        try:
            api_key = payload.api_key or get_api_key(connection.secret_ref)
        except SecretStoreError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if not api_key:
            raise HTTPException(status_code=409, detail="Stored API key is missing")
        result = discover_models(connection.base_url, api_key, connection.custom_headers)
        connection.last_checked_at = datetime.now(timezone.utc)
        if result.success:
            connection.discovered_models = result.models
            connection.status = "connected"
            connection.last_error = None
            connection.capabilities = {**connection.capabilities, "model_discovery": True}
        elif connection.manual_models and result.category != "invalid_credentials":
            connection.discovered_models = []
            connection.status = "manual"
            connection.last_error = result.message
            connection.capabilities = {**connection.capabilities, "model_discovery": False}
        else:
            db.rollback()
            raise HTTPException(
                status_code=400,
                detail={"message": result.message, "category": result.category, "retryable": result.retryable},
            )
    if should_probe:
        try:
            api_key = payload.api_key or get_api_key(connection.secret_ref)
        except SecretStoreError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if not api_key:
            raise HTTPException(status_code=409, detail="Stored API key is missing")
        protocol_result = probe_protocol(
            connection.base_url, api_key, connection.api_protocol,
            connection.default_model or next(iter(connection.manual_models or connection.discovered_models), None),
            connection.custom_headers,
        )
        if not protocol_result.success:
            db.rollback()
            raise HTTPException(status_code=400, detail={
                "message": protocol_result.message, "category": protocol_result.category,
                "retryable": protocol_result.retryable,
            })
        connection.capabilities = {**connection.capabilities, connection.api_protocol: True}
    if payload.api_key is not None:
        _save_or_503(connection.secret_ref, payload.api_key)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="A connection with this name already exists") from exc
    db.refresh(connection)
    return connection


@router.post("/{connection_id}/test", response_model=ConnectionTestResult)
def test_connection(connection_id: str, db: Session = Depends(get_db)) -> ConnectionTestResult:
    connection = _require_connection(db, connection_id)
    try:
        api_key = get_api_key(connection.secret_ref)
    except SecretStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not api_key:
        result = ConnectionTestResult(
            success=False,
            category="missing_secret",
            message="The API key is missing from the operating-system credential store",
            retryable=False,
        )
    else:
        result = probe_protocol(
            connection.base_url, api_key, connection.api_protocol,
            connection.default_model or next(iter(connection.manual_models or connection.discovered_models), None),
            connection.custom_headers,
        )
    connection.last_checked_at = datetime.now(timezone.utc)
    connection.status = "connected" if result.success else result.category
    connection.last_error = None if result.success else result.message
    if result.success:
        connection.capabilities = {**connection.capabilities, connection.api_protocol: True}
    db.commit()
    return result


@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_connection(connection_id: str, db: Session = Depends(get_db)) -> Response:
    connection = _require_connection(db, connection_id)
    try:
        delete_api_key(connection.secret_ref)
    except SecretStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    db.delete(connection)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
