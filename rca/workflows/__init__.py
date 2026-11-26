"""Workflow variant registry and public build_workflow entry point."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from rca.contracts.provider import CompleteFn
from rca.workflows.base import build_base_workflow
from rca.workflows.mas import build_mas_workflow
from rca.workflows.mas_validation import (
    AgentExecutionTrace,
    EvidenceClaimIssue,
    MasTracer,
    MasValidationError,
    RecordingMasTracer,
    build_validated_mas_workflow,
)
from rca.workflows.optimal import build_optimal_workflow

WorkflowBuilder = Callable[..., Any]

_REGISTRY_LOCK = threading.RLock()
_REGISTRY: dict[str, WorkflowBuilder] = {}


def register_variant(
    name: str,
    builder: WorkflowBuilder,
    *,
    replace: bool = False,
) -> None:
    """Register a workflow builder for a named RCA variant."""
    key = name.strip()
    if not key:
        raise ValueError("Workflow variant name must not be empty.")
    if not callable(builder):
        raise TypeError("Workflow builder must be callable.")
    with _REGISTRY_LOCK:
        if key in _REGISTRY and not replace:
            raise ValueError(
                f"Workflow variant {key!r} is already registered. Pass replace=True to override."
            )
        _REGISTRY[key] = builder


def unregister_variant(name: str) -> None:
    """Remove a registered workflow variant (primarily for tests)."""
    with _REGISTRY_LOCK:
        _REGISTRY.pop(name, None)


def list_variants() -> tuple[str, ...]:
    """Return sorted names of all registered workflow variants."""
    with _REGISTRY_LOCK:
        return tuple(sorted(_REGISTRY))


def build_workflow(variant: str, *, complete: CompleteFn, **kwargs: Any) -> Any:
    """Build and return a compiled workflow graph for the requested variant."""
    if not callable(complete):
        raise TypeError("complete must be a callable accepting (prompt_path, payload) -> str.")
    with _REGISTRY_LOCK:
        builder = _REGISTRY.get(variant)
    if builder is None:
        known = ", ".join(list_variants()) or "none"
        raise ValueError(f"Unknown workflow variant {variant!r}. Registered variants: {known}.")
    return builder(complete=complete, **kwargs)


register_variant("base", build_base_workflow)
register_variant("optimal", build_optimal_workflow)
register_variant("mas", build_mas_workflow)

__all__ = [
    "AgentExecutionTrace",
    "EvidenceClaimIssue",
    "MasTracer",
    "MasValidationError",
    "RecordingMasTracer",
    "WorkflowBuilder",
    "build_validated_mas_workflow",
    "build_workflow",
    "list_variants",
    "register_variant",
    "unregister_variant",
]
