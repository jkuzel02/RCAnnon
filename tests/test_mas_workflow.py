"""Focused tests for the four-call parallel MAS LangGraph workflow."""

from __future__ import annotations

import copy
import json
import threading
import time
from typing import Any
from unittest.mock import Mock

import pytest

from rca.contracts.provider import ProviderResponse
from rca.providers import EInfraProvider, ProviderConfig, make_sync_complete
from rca.workflows import build_workflow
from rca.workflows.mas import build_mas_workflow, extract_resource_limits

SPECIALIST_REPORTS: dict[str, dict[str, Any]] = {
    "k8s-config-events": {
        "agent": "k8s-config-events",
        "anomalies": [
            {
                "resource": "Deployment/api",
                "field": "spec.template.spec.containers[0].resources.limits.memory",
                "issue": "Memory limit 64Mi is below expected working set.",
                "severity": "high",
            }
        ],
        "suspicious_resources": ["Deployment/api"],
        "significant_events": [
            {
                "reason": "OOMKilled",
                "involved_object": "Pod/api",
                "message": "Container web exceeded memory limit.",
            }
        ],
        "preliminary_assessment": "Memory limit appears too low.",
    },
    "logs-connectivity": {
        "agent": "logs-connectivity",
        "log_findings": [
            {"source": "web", "level": "error", "summary": "Allocated 56 MiB before OOM."}
        ],
        "connectivity_findings": [
            {"target": "Service/api", "status": "failed", "details": "Endpoint unreachable."}
        ],
        "runtime_failure_mode": "oom",
        "preliminary_assessment": "Process terminates from memory exhaustion.",
    },
    "metrics-telemetry": {
        "agent": "metrics-telemetry",
        "resource_findings": [
            {
                "metric_name": "container_memory_working_set_bytes",
                "observed_value": "uncollected",
                "limit_value": "64Mi",
                "status": "uncollected",
                "analysis": "No metrics series supplied.",
            }
        ],
        "throttling_detected": False,
        "saturation_identified": None,
        "preliminary_assessment": "Runtime telemetry absent; limit is 64Mi.",
    },
}

FINAL_RCA: dict[str, Any] = {
    "cause": "Deployment/api limits container web to 64Mi while it needs more memory.",
    "causal_chain": "Memory limit triggers OOMKilled restarts and drops Service/api connectivity.",
    "supporting_evidence": [
        "Deployment/api container web memory limit is 64Mi",
        "OOMKilled event recorded for Pod/api",
    ],
    "remediation": "Increase the web container memory limit above the working set.",
    "recovery_check": "Confirm Pod/api stays Ready with zero OOMKilled restarts.",
}


def _sample_context() -> dict[str, Any]:
    return {
        "incident": "The api deployment restarts repeatedly and drops requests.",
        "time_window": None,
        "resources": [
            {
                "kind": "Deployment",
                "metadata": {"name": "api", "namespace": "rca-test"},
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "web",
                                    "resources": {
                                        "requests": {"memory": "32Mi"},
                                        "limits": {"memory": "64Mi"},
                                    },
                                }
                            ]
                        }
                    }
                },
            }
        ],
        "events": [{"reason": "OOMKilled", "involved_object": "Pod/api"}],
        "logs": [{"container": "web", "message": "retained_mib=56"}],
        "metrics": [],
        "checks": [{"target": "Service/api", "status": "failed"}],
    }


def _canned_response(prompt_path: str) -> str:
    agent = prompt_path.removeprefix("mas/").removesuffix(".md")
    if agent in SPECIALIST_REPORTS:
        return json.dumps(SPECIALIST_REPORTS[agent])
    if prompt_path == "mas/coordinator.md":
        return json.dumps(FINAL_RCA)
    raise AssertionError(f"Unexpected prompt path: {prompt_path}")


def test_mas_parallel_barrier_and_deterministic_fan_in() -> None:
    barrier = threading.Barrier(3, timeout=2.0)
    lock = threading.Lock()
    call_log: list[tuple[str, dict[str, Any]]] = []

    def complete(prompt_path: str, payload: dict[str, Any]) -> str:
        if prompt_path != "mas/coordinator.md":
            barrier.wait()
        with lock:
            call_log.append((prompt_path, copy.deepcopy(payload)))
        return _canned_response(prompt_path)

    ctx = _sample_context()
    graph = build_workflow("mas", complete=complete)
    result = graph.invoke({"context": ctx})

    assert len(call_log) == 4
    specialist_prompts = {prompt for prompt, _ in call_log[:-1]}
    assert specialist_prompts == {
        "mas/k8s-config-events.md",
        "mas/logs-connectivity.md",
        "mas/metrics-telemetry.md",
    }
    assert call_log[-1][0] == "mas/coordinator.md"
    assert call_log[-1][1] == {
        "context": ctx,
        "agent_reports": SPECIALIST_REPORTS,
    }
    assert result["root-cause"] == FINAL_RCA


def test_mas_sequential_provider_cap_matches_parallel_payloads() -> None:
    ctx = _sample_context()

    parallel_payloads: dict[str, dict[str, Any]] = {}
    parallel_lock = threading.Lock()

    def parallel_complete(prompt_path: str, payload: dict[str, Any]) -> str:
        with parallel_lock:
            parallel_payloads[prompt_path] = copy.deepcopy(payload)
        return _canned_response(prompt_path)

    parallel_result = build_mas_workflow(complete=parallel_complete).invoke({"context": ctx})

    # Explicit max_concurrency=1 enforces sequential specialist calls.
    active = 0
    max_active = 0
    seq_lock = threading.Lock()
    seq_payloads: dict[str, dict[str, Any]] = {}

    def seq_complete(prompt_path: str, payload: dict[str, Any]) -> str:
        nonlocal active, max_active
        with seq_lock:
            active += 1
            max_active = max(max_active, active)
            seq_payloads[prompt_path] = copy.deepcopy(payload)
        try:
            time.sleep(0.01)
            return _canned_response(prompt_path)
        finally:
            with seq_lock:
                active -= 1

    seq_result = build_workflow("mas", complete=seq_complete, max_concurrency=1).invoke(
        {"context": ctx}
    )
    assert max_active == 1
    assert seq_payloads == parallel_payloads
    assert seq_result["root-cause"] == parallel_result["root-cause"]

    # Bound EInfraProvider callable carrying config.max_concurrency=1 also clamps concurrency.
    provider = EInfraProvider(ProviderConfig(api_key="sk-test", max_concurrency=1))
    provider_active = 0
    provider_max_active = 0
    provider_lock = threading.Lock()
    provider_payloads: dict[str, dict[str, Any]] = {}

    def fake_complete_response(prompt_path: str, payload: dict[str, Any]) -> ProviderResponse:
        nonlocal provider_active, provider_max_active
        with provider_lock:
            provider_active += 1
            provider_max_active = max(provider_max_active, provider_active)
            provider_payloads[prompt_path] = copy.deepcopy(payload)
        try:
            time.sleep(0.01)
            return ProviderResponse(
                text=_canned_response(prompt_path),
                model="deepseek-v4.1-flash",
                request_id=None,
                attempt_id="test",
                attempts=1,
                duration_seconds=0.01,
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                finish_status="stop",
            )
        finally:
            with provider_lock:
                provider_active -= 1

    provider.complete_response = fake_complete_response  # type: ignore[method-assign]
    bound_complete = make_sync_complete(provider)
    bound_result = build_workflow("mas", complete=bound_complete).invoke({"context": ctx})
    assert provider_max_active == 1
    assert provider_payloads == parallel_payloads
    assert bound_result["root-cause"] == parallel_result["root-cause"]


def test_mas_zero_observation_context_preserves_empty_fields() -> None:
    empty_context = {
        "incident": "Unknown failure with no collected telemetry.",
        "time_window": None,
        "resources": [],
        "events": [],
        "logs": [],
        "metrics": [],
        "checks": [],
    }
    recorded: dict[str, dict[str, Any]] = {}
    lock = threading.Lock()

    def complete(prompt_path: str, payload: dict[str, Any]) -> str:
        with lock:
            recorded[prompt_path] = copy.deepcopy(payload)
        return _canned_response(prompt_path)

    result = build_mas_workflow(complete=complete).invoke({"context": empty_context})

    assert recorded["mas/k8s-config-events.md"] == {"resources": [], "events": []}
    assert recorded["mas/logs-connectivity.md"] == {"logs": [], "checks": []}
    assert recorded["mas/metrics-telemetry.md"] == {
        "metrics": [],
        "time_window": None,
        "resource_limits": [],
    }
    assert recorded["mas/coordinator.md"] == {
        "context": empty_context,
        "agent_reports": SPECIALIST_REPORTS,
    }
    assert result["root-cause"] == FINAL_RCA


def test_extract_resource_limits_handles_workloads_and_empty_containers() -> None:
    resources = [
        {"kind": "Namespace", "metadata": {"name": "rca-test"}},
        {
            "kind": "Deployment",
            "metadata": {"name": "api", "namespace": "rca-test"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "web",
                                "resources": {
                                    "requests": {"cpu": "25m"},
                                    "limits": {"memory": "64Mi"},
                                },
                            },
                            {"name": "sidecar"},
                        ]
                    }
                }
            },
        },
        {
            "kind": "Pod",
            "metadata": {"name": "worker", "namespace": "rca-test"},
            "spec": {
                "containers": [
                    {
                        "name": "runner",
                        "resources": {"limits": {"cpu": "200m"}},
                    }
                ]
            },
        },
        {
            "kind": "CronJob",
            "metadata": {"name": "cleanup", "namespace": "rca-test"},
            "spec": {
                "jobTemplate": {
                    "spec": {
                        "template": {
                            "spec": {
                                "containers": [
                                    {
                                        "name": "job",
                                        "resources": {"requests": {"memory": "16Mi"}},
                                    }
                                ]
                            }
                        }
                    }
                }
            },
        },
    ]

    assert extract_resource_limits(resources) == [
        {
            "resource": "Deployment/api",
            "namespace": "rca-test",
            "container": "web",
            "requests": {"cpu": "25m"},
            "limits": {"memory": "64Mi"},
        },
        {
            "resource": "Pod/worker",
            "namespace": "rca-test",
            "container": "runner",
            "requests": {},
            "limits": {"cpu": "200m"},
        },
        {
            "resource": "CronJob/cleanup",
            "namespace": "rca-test",
            "container": "job",
            "requests": {"memory": "16Mi"},
            "limits": {},
        },
    ]


@pytest.mark.parametrize(
    "bad_specialist_output",
    [
        "not json",
        "{}",
        json.dumps({**SPECIALIST_REPORTS["logs-connectivity"], "extra": 1}),
        json.dumps({**SPECIALIST_REPORTS["logs-connectivity"], "agent": "wrong-agent"}),
    ],
)
def test_mas_invalid_specialist_blocks_coordinator(bad_specialist_output: str) -> None:
    called_prompts: list[str] = []
    lock = threading.Lock()

    def complete(prompt_path: str, _payload: dict[str, Any]) -> str:
        with lock:
            called_prompts.append(prompt_path)
        if prompt_path == "mas/logs-connectivity.md":
            return bad_specialist_output
        return _canned_response(prompt_path)

    with pytest.raises(ValueError):
        build_mas_workflow(complete=complete).invoke({"context": _sample_context()})

    assert "mas/coordinator.md" not in called_prompts


def test_mas_specialist_provider_failure_propagates_and_blocks_coordinator() -> None:
    sentinel = TimeoutError("e-infra specialist timeout")
    called_prompts: list[str] = []
    lock = threading.Lock()

    def complete(prompt_path: str, _payload: dict[str, Any]) -> str:
        with lock:
            called_prompts.append(prompt_path)
        if prompt_path == "mas/k8s-config-events.md":
            raise sentinel
        return _canned_response(prompt_path)

    with pytest.raises(TimeoutError) as exc_info:
        build_mas_workflow(complete=complete).invoke({"context": _sample_context()})

    assert exc_info.value is sentinel
    assert "mas/coordinator.md" not in called_prompts


@pytest.mark.parametrize(
    "bad_coordinator_output",
    [
        "```json\n" + json.dumps(FINAL_RCA) + "\n```",
        json.dumps(FINAL_RCA)[:-4],
        json.dumps({k: v for k, v in FINAL_RCA.items() if k != "recovery_check"}),
        json.dumps({**FINAL_RCA, "unexpected": "field"}),
    ],
)
def test_mas_invalid_coordinator_response_raises_value_error_without_retry(
    bad_coordinator_output: str,
) -> None:
    calls: list[str] = []
    lock = threading.Lock()

    def complete(prompt_path: str, _payload: dict[str, Any]) -> str:
        with lock:
            calls.append(prompt_path)
        if prompt_path == "mas/coordinator.md":
            return bad_coordinator_output
        return _canned_response(prompt_path)

    with pytest.raises(ValueError):
        build_mas_workflow(complete=complete).invoke({"context": _sample_context()})

    assert len(calls) == 4
    assert calls[-1] == "mas/coordinator.md"


def test_mas_repeated_concurrent_runs_are_consistent() -> None:
    delays = {
        "mas/k8s-config-events.md": 0.015,
        "mas/logs-connectivity.md": 0.005,
        "mas/metrics-telemetry.md": 0.01,
    }
    ctx = _sample_context()

    for _ in range(5):
        calls: list[tuple[str, dict[str, Any]]] = []
        lock = threading.Lock()

        def complete(prompt_path: str, payload: dict[str, Any]) -> str:
            if prompt_path in delays:
                time.sleep(delays[prompt_path])
            with lock:
                calls.append((prompt_path, copy.deepcopy(payload)))
            return _canned_response(prompt_path)

        result = build_workflow("mas", complete=complete).invoke({"context": ctx})
        assert len(calls) == 4
        assert calls[-1][0] == "mas/coordinator.md"
        assert calls[-1][1]["agent_reports"] == SPECIALIST_REPORTS
        assert result["root-cause"] == FINAL_RCA


@pytest.mark.parametrize("bad_concurrency", [0, -1, True])
def test_mas_invalid_max_concurrency_rejected(bad_concurrency: Any) -> None:
    with pytest.raises(ValueError, match="max_concurrency"):
        build_mas_workflow(complete=Mock(return_value="ok"), max_concurrency=bad_concurrency)
