"""Focused tests for the one-call baseline LangGraph workflow."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from rca.workflows import build_workflow
from rca.workflows.base import BASE_PROMPT_PATH, build_base_workflow


def test_base_workflow_preserves_exact_text_and_partial_context() -> None:
    partial_context = {
        "incident": "API pod restarts under steady traffic.",
        "time_window": None,
        "resources": [{"kind": "Deployment", "metadata": {"name": "api"}}],
        "events": [],
        "logs": [],
        "metrics": [],
        "checks": [],
    }
    raw_text = "  Free-form diagnosis:\n- Memory limit is too low.\n  "
    complete = Mock(return_value=raw_text)

    graph = build_base_workflow(complete=complete)
    result = graph.invoke({"context": partial_context})

    complete.assert_called_once_with(BASE_PROMPT_PATH, {"context": partial_context})
    assert result["root-cause"] == raw_text


def test_base_workflow_via_registry_propagates_provider_error_unchanged() -> None:
    sentinel = RuntimeError("upstream provider unavailable")
    complete = Mock(side_effect=sentinel)

    graph = build_workflow("base", complete=complete)
    with pytest.raises(RuntimeError) as exc_info:
        graph.invoke({"context": {"incident": "outage"}})

    assert exc_info.value is sentinel
    assert complete.call_count == 1
