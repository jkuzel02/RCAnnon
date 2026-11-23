"""Focused tests for the one-call optimized LangGraph workflow."""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from rca.workflows import build_workflow
from rca.workflows.optimal import OPTIMAL_PROMPT_PATH, build_optimal_workflow

VALID_RCA = {
    "cause": "Deployment/api limits container web to 64Mi while it allocates 128Mi.",
    "causal_chain": "The container exceeds its 64Mi memory limit and is OOMKilled.",
    "supporting_evidence": [
        "Deployment/api spec.template.spec.containers[0].resources.limits.memory=64Mi",
        "Runtime logs and metrics were not collected; diagnosis is grounded in manifests.",
    ],
    "remediation": "Increase the web container memory limit above the working set.",
    "recovery_check": "Verify pods remain Ready with zero OOMKilled restarts.",
}


def test_optimal_receives_byte_equivalent_context_as_base_and_validates_clean_json() -> None:
    partial_context = {
        "incident": "The api deployment restarts repeatedly.",
        "time_window": None,
        "resources": [{"kind": "Deployment", "metadata": {"name": "api"}}],
        "events": [],
        "logs": [],
        "metrics": [],
        "checks": [],
    }
    base_complete = Mock(return_value="plain text")
    optimal_complete = Mock(return_value=json.dumps(VALID_RCA))

    build_workflow("base", complete=base_complete).invoke({"context": partial_context})
    result = build_workflow("optimal", complete=optimal_complete).invoke(
        {"context": partial_context}
    )

    optimal_complete.assert_called_once_with(OPTIMAL_PROMPT_PATH, {"context": partial_context})
    assert json.dumps(base_complete.call_args.args[1], sort_keys=True) == json.dumps(
        optimal_complete.call_args.args[1], sort_keys=True
    )
    assert result["root-cause"] == VALID_RCA


@pytest.mark.parametrize(
    "bad_output",
    [
        "```json\n" + json.dumps(VALID_RCA) + "\n```",
        json.dumps(VALID_RCA)[:-5],
        json.dumps([VALID_RCA]),
        json.dumps({**VALID_RCA, "extra_field": "not allowed"}),
        json.dumps({k: v for k, v in VALID_RCA.items() if k != "recovery_check"}),
        json.dumps({**VALID_RCA, "cause": "   "}),
        json.dumps({**VALID_RCA, "supporting_evidence": []}),
        json.dumps({**VALID_RCA, "supporting_evidence": "not-a-list"}),
    ],
)
def test_optimal_rejects_malformed_or_schema_invalid_outputs_without_retry(
    bad_output: str,
) -> None:
    complete = Mock(return_value=bad_output)
    graph = build_optimal_workflow(complete=complete)

    with pytest.raises(ValueError):
        graph.invoke({"context": {"incident": "outage"}})

    assert complete.call_count == 1


def test_optimal_propagates_provider_errors_unchanged() -> None:
    sentinel = TimeoutError("e-infra deadline exceeded")
    complete = Mock(side_effect=sentinel)
    graph = build_workflow("optimal", complete=complete)

    with pytest.raises(TimeoutError) as exc_info:
        graph.invoke({"context": {"incident": "outage"}})

    assert exc_info.value is sentinel
    assert complete.call_count == 1
