"""Connection-level wire protocol configuration."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal

@dataclass(slots=True)
class ProviderConfig:
    provider: str
    base_url: str
    secret_ref: str
    model_id: str
    model_connection_id: str | None = None
    thinking_level: str = "auto"
    custom_headers: dict[str, str] = field(default_factory=dict)
    api_protocol: Literal["chat_completions", "responses"] = "chat_completions"
    max_output_tokens: int = 8_000
