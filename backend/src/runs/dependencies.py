"""Late-bound compatibility dependencies for the run domain."""

from typing import Any

from src.model.gateway import ProviderConfig


def build_model_call(config: ProviderConfig) -> Any:
    # Imported at call time so existing extensions that replace
    # src.runs.service.build_model_call keep controlling every run.
    from . import service as run_service

    return run_service.build_model_call(config)
