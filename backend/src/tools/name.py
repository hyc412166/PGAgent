"""Structured tool identity shared by planning, routing and persistence."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True, order=True)
class ToolName:
    """Canonical name independent from the provider-facing wire name."""

    namespace: str
    name: str

    def __post_init__(self) -> None:
        namespace = self.namespace.strip()
        name = self.name.strip()
        if not namespace or not name:
            raise ValueError("tool namespace and name must not be empty")
        object.__setattr__(self, "namespace", namespace)
        object.__setattr__(self, "name", name)

    @classmethod
    def builtin(cls, name: str) -> "ToolName":
        return cls("builtin", name)

    @classmethod
    def external(cls, owner: str, name: str) -> "ToolName":
        return cls(owner, name)

    @property
    def canonical(self) -> str:
        return f"{self.namespace}.{self.name}"

    def __str__(self) -> str:
        return self.canonical
