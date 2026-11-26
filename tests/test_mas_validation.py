"""Tests for MAS evidence claim validation, reference checks, partial coverage, and tracing."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from rca.api import ApiConfig, RcaApiService, RunStore
from rca.api.service import ApiProviderUnavailableError, ApiTimeoutError
from rca.contracts.provider import ProviderTimeoutError
from rca.contracts.runtime import (
    EvidenceSnapshot,
    SourceCoverage,
    TargetSelector,
    compute_scope_hash,
    derive_default_coverage,
    utc_now_iso,
)
from rca.contracts.schemas import validate_observations
from rca.workflows import (
    MasValidationError,
    RecordingMasTracer,
    build_validated_mas_workflow,
    build_workflow,
)
from rca.workflows.mas_validation import (
    detect_affirmative_source_claims,
    extract_known_references,
)

SECRET_SENTINEL = "sk-sentinel-mas-secret-998877"


def _sparse_context() -> dict[str, Any]:
    return {
        "incident": "The api deployment fails readiness and restarts under load.",
        "time_window": None,
        "resources": [
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "api", "namespace": "rca-test"},
                "spec": {
                    "template": {
                        "metadata": {"labels": {"app": "api"}},
                        "spec": {
                            "containers": [
                                {
                                    "name": "web",
                                    "image": "python:3.12.8-slim",
                                    "resources": {
                                        "requests": {"cpu": "25m", "memory": "32Mi"},
                                        "limits": {"cpu": "500m", "memory": "64Mi"},
                                    },
                                }
                            ]
                        },
                    }
                },
            },
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": "api", "namespace": "rca-test"},
                "spec": {
                    "ports": [{"name": "http", "port": 80, "targetPort": 8080}],
                    "selector": {"app": "api"},
                },
            },
        ],
        "events": [],
        "logs": [],
        "metrics": [],
        "checks": [],
    }


def _valid_sparse_reports() -> dict[str, dict[str, Any]]:
    return {
        "k8s-config-events": {
            "agent": "k8s-config-events",
            "anomalies": [
                {
                    "resource": "Deployment/api",
                    "field": "spec.template.spec.containers[0].resources.limits.memory",
                    "issue": "Memory limit 64Mi may be insufficient for normal working set.",
                    "severity": "high",
                }
            ],
            "suspicious_resources": ["Deployment/api"],
            "significant_events": [],
            "preliminary_assessment": (
                "Deployment/api restricts web memory to 64Mi; no Kubernetes events were provided."
            ),
        },
        "logs-connectivity": {
            "agent": "logs-connectivity",
            "log_findings": [],
            "connectivity_findings": [],
            "runtime_failure_mode": "unknown",
            "preliminary_assessment": (
                "No container logs or connectivity checks were collected for Deployment/api."
            ),
        },
        "metrics-telemetry": {
            "agent": "metrics-telemetry",
            "resource_findings": [
                {
                    "metric_name": "container_memory_working_set_bytes",
                    "observed_value": "uncollected",
                    "limit_value": "64Mi",
                    "status": "uncollected",
                    "analysis": "Metrics were not collected; missing telemetry is not zero usage.",
                }
            ],
            "throttling_detected": False,
            "saturation_identified": None,
            "preliminary_assessment": "Runtime telemetry absent; declared memory limit is 64Mi.",
        },
    }


def _valid_sparse_rca() -> dict[str, Any]:
    return {
        "cause": "Deployment/api configures a low 64Mi memory limit for container web.",
        "causal_chain": (
            "With a 64Mi memory limit on Deployment/api, memory allocations above 64Mi "
            "trigger container termination; runtime logs and metrics were unavailable."
        ),
        "supporting_evidence": [
            "Deployment/api spec.template.spec.containers[0].resources.limits.memory is 64Mi.",
            "Events, logs, metrics, and checks were not collected during this snapshot.",
        ],
        "remediation": "Increase the web container memory limit in Deployment/api to 256Mi.",
        "recovery_check": "Confirm Pod/api-0 becomes Ready without further restarts.",
    }


def _make_api_service(
    tmp_path: Path,
    *,
    complete: Any,
    coverage_overrides: dict[str, SourceCoverage] | None = None,
    tracer: Any = None,
) -> tuple[RcaApiService, RunStore]:
    cfg = ApiConfig(
        ui_tokens=frozenset({"ui-token"}),
        operator_tokens=frozenset({"op-token"}),
        allowed_scopes={"lab": frozenset({"rca-test"})},
        db_path=tmp_path / "mas_validation.db",
        run_deadline_seconds=5.0,
        extra_redaction_secrets=(SECRET_SENTINEL,),
    )
    store = RunStore(cfg.db_path)
    store.initialize()

    def collector(target: TargetSelector, incident: str) -> EvidenceSnapshot:
        ctx = _sparse_context()
        ctx["incident"] = incident
        cov = derive_default_coverage(ctx)
        if coverage_overrides:
            cov.update(coverage_overrides)
        return EvidenceSnapshot(
            snapshot_id="snap_sparse_01",
            scope_hash=compute_scope_hash(target.context_alias, target.namespaces, ctx),
            source_type="live",
            context_alias=target.context_alias,
            namespaces=list(target.namespaces),
            collected_at=utc_now_iso(),
            observed_at=utc_now_iso(),
            context=ctx,
            coverage=cov,
        )

    service = RcaApiService(cfg, store, complete, collector=collector, tracer=tracer)
    return service, store


def test_validate_observations_rejects_fabricated_empty_source_findings() -> None:
    with pytest.raises(ValueError, match="significant_events"):
        validate_observations(
            {"significant_events": [{"reason": "OOMKilled", "involved_object": "Pod/api", "message": "oom"}]},
            {"resources": [], "events": []},
        )
    with pytest.raises(ValueError, match="log_findings"):
        validate_observations(
            {"log_findings": [{"source": "web", "level": "error", "summary": "crash"}]},
            {"logs": [], "checks": []},
        )
    with pytest.raises(ValueError, match="connectivity_findings"):
        validate_observations(
            {"connectivity_findings": [{"target": "Service/api", "status": "failed", "details": "timeout"}]},
            {"logs": [], "checks": []},
        )
    with pytest.raises(ValueError, match="metrics"):
        validate_observations(
            {"throttling_detected": False, "saturation_identified": "oom", "resource_findings": []},
            {"metrics": []},
        )


@pytest.mark.parametrize(
    ("specialist", "bad_report"),
    [
        (
            "k8s-config-events",
            {
                "agent": "k8s-config-events",
                "anomalies": [],
                "suspicious_resources": [],
                "significant_events": [
                    {
                        "reason": "OOMKilling",
                        "involved_object": "Pod/api",
                        "message": "Memory cgroup out of memory",
                    }
                ],
                "preliminary_assessment": "Events show OOM.",
            },
        ),
        (
            "logs-connectivity",
            {
                "agent": "logs-connectivity",
                "log_findings": [
                    {"source": "web", "level": "error", "summary": "Crash on startup."}
                ],
                "connectivity_findings": [],
                "runtime_failure_mode": "crash",
                "preliminary_assessment": "Logs indicate crash.",
            },
        ),
        (
            "metrics-telemetry",
            {
                "agent": "metrics-telemetry",
                "resource_findings": [
                    {
                        "metric_name": "container_memory_working_set_bytes",
                        "observed_value": "64 MiB",
                        "limit_value": "64Mi",
                        "status": "saturated",
                        "analysis": "Reached limit.",
                    }
                ],
                "throttling_detected": False,
                "saturation_identified": "container_memory_limit_exceeded",
                "preliminary_assessment": "Memory saturated.",
            },
        ),
        (
            "metrics-telemetry",
            {
                "agent": "metrics-telemetry",
                "resource_findings": [],
                "throttling_detected": False,
                "saturation_identified": None,
                "preliminary_assessment": (
                    "Prometheus metrics confirm memory working set reached 64Mi, "
                    "although no events were collected."
                ),
            },
        ),
    ],
)
def test_fabricated_empty_inputs_stop_strict_coordinator_and_trace_timing(
    specialist: str,
    bad_report: dict[str, Any],
) -> None:
    reports = _valid_sparse_reports()
    reports[specialist] = bad_report
    called: list[str] = []
    tracer = RecordingMasTracer()

    def complete(prompt_path: str, _payload: dict[str, Any]) -> str:
        called.append(prompt_path)
        agent = prompt_path.removeprefix("mas/").removesuffix(".md")
        if agent in reports:
            return json.dumps(reports[agent])
        return json.dumps(_valid_sparse_rca())

    graph = build_validated_mas_workflow(complete=complete, tracer=tracer)
    with pytest.raises(MasValidationError) as exc_info:
        graph.invoke({"context": _sparse_context()})

    assert exc_info.value.error_category == "unsupported_observation"
    assert "mas/coordinator.md" not in called
    records = tracer.records
    failed_records = [r for r in records if r.agent == specialist and r.status == "failed"]
    blocked_records = [r for r in records if r.agent == "coordinator" and r.status == "blocked"]
    assert len(failed_records) == 1
    assert failed_records[0].error_category == "unsupported_observation"
    assert failed_records[0].duration_seconds >= 0.0
    assert len(blocked_records) == 1
    assert blocked_records[0].error_category == "blocked_by_specialist"


def test_affirmative_claim_detector_distinguishes_disclaimers_from_claims() -> None:
    truthful = (
        "No runtime metrics were provided. Container logs were not collected; "
        "CPU throttling cannot be confirmed without metrics."
    )
    assert detect_affirmative_source_claims(truthful, {"logs", "metrics", "events", "checks"}) == []

    fabricated = "Container logs show invalid APP_MODE. DNS lookup succeeded for backend."
    issues = detect_affirmative_source_claims(fabricated, {"logs", "checks"})
    assert {issue.source for issue in issues} == {"logs", "checks"}
    assert all(issue.category == "unsupported_observation" for issue in issues)


def test_reference_verification_catches_hallucinated_resources_and_allows_valid_ones() -> None:
    ctx = _sparse_context()
    ctx["resources"].append(
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": "data", "namespace": "rca-test"},
            "spec": {"storageClassName": "rca-missing-storage"},
        }
    )
    known = extract_known_references(ctx)
    assert known.knows_ref("Deployment", "api")
    assert known.knows_ref("Pod", "api-6b8d9c7f-abc12")
    assert known.knows_ref("StorageClass", "rca-missing-storage")
    assert not known.knows_ref("Deployment", "phantom-database")

    reports = _valid_sparse_reports()
    bad_rca = {
        **_valid_sparse_rca(),
        "supporting_evidence": ["Deployment/phantom-database failed health checks."],
    }

    def complete(prompt_path: str, _payload: dict[str, Any]) -> str:
        agent = prompt_path.removeprefix("mas/").removesuffix(".md")
        if agent in reports:
            return json.dumps(reports[agent])
        return json.dumps(bad_rca)

    tracer = RecordingMasTracer()
    graph = build_validated_mas_workflow(complete=complete, tracer=tracer)
    with pytest.raises(MasValidationError) as exc_info:
        graph.invoke({"context": ctx})

    assert exc_info.value.agent == "coordinator"
    assert exc_info.value.error_category == "unverified_reference"
    coord_traces = [r for r in tracer.records if r.agent == "coordinator"]
    assert len(coord_traces) == 1
    assert coord_traces[0].status == "failed"
    assert coord_traces[0].error_category == "unverified_reference"


def test_reference_verification_checks_populated_logs_and_metrics() -> None:
    ctx = _sparse_context()
    ctx["logs"] = [{"pod": "api-123", "container": "web", "message": "listening on 8080"}]
    ctx["metrics"] = [{"metric": "container_memory_working_set_bytes", "pod": "api-123", "value": 64}]

    reports = _valid_sparse_reports()
    reports["metrics-telemetry"] = {
        "agent": "metrics-telemetry",
        "resource_findings": [
            {
                "metric_name": "invented_disk_iops_total",
                "observed_value": "999",
                "limit_value": None,
                "status": "saturated",
                "analysis": "Invented metric.",
            }
        ],
        "throttling_detected": False,
        "saturation_identified": "disk_saturation",
        "preliminary_assessment": "Disk saturated.",
    }

    def complete(prompt_path: str, _payload: dict[str, Any]) -> str:
        agent = prompt_path.removeprefix("mas/").removesuffix(".md")
        if agent in reports:
            return json.dumps(reports[agent])
        return json.dumps(_valid_sparse_rca())

    with pytest.raises(MasValidationError) as exc_info:
        build_validated_mas_workflow(complete=complete).invoke({"context": ctx})
    assert exc_info.value.error_category == "unverified_reference"


def test_deterministic_valid_sparse_fixture_uses_four_completions_with_limitations(
    tmp_path: Path,
) -> None:
    reports = _valid_sparse_reports()
    rca = _valid_sparse_rca()
    calls: list[str] = []
    tracer = RecordingMasTracer()

    def complete(prompt_path: str, _payload: dict[str, Any]) -> str:
        calls.append(prompt_path)
        agent = prompt_path.removeprefix("mas/").removesuffix(".md")
        if agent in reports:
            return json.dumps(reports[agent])
        return json.dumps(rca)

    coverage_overrides = {
        "events": SourceCoverage(status="forbidden", count=0, reason="rbac denied"),
        "logs": SourceCoverage(status="truncated", count=0, reason="log endpoint unreachable", truncated=True),
        "metrics": SourceCoverage(status="unreachable", count=0, reason="prometheus down"),
    }
    service, store = _make_api_service(
        tmp_path,
        complete=complete,
        coverage_overrides=coverage_overrides,
        tracer=tracer,
    )
    try:
        run_result = asyncio.run(
            service.execute_run(
                request_id="req_sparse_01",
                variant="mas",
                target=TargetSelector(context_alias="lab", namespaces=["rca-test"]),
                incident="The api pod is restarting.",
            )
        )
    finally:
        service.shutdown()
        store.close()

    assert len(calls) == 4
    assert run_result.status == "qualified"
    assert run_result.usage.logical_calls == 4
    assert run_result.result == rca
    assert any("events: forbidden" in lim for lim in run_result.limitations)
    assert any("metrics: unreachable" in lim for lim in run_result.limitations)
    assert len(tracer.records) == 4
    assert all(record.status == "passed" for record in tracer.records)
    assert all(record.duration_seconds >= 0.0 for record in tracer.records)


def test_sentinel_redaction_and_payload_free_traces_on_validation_failure(
    tmp_path: Path,
) -> None:
    tracer = RecordingMasTracer()
    reports = _valid_sparse_reports()
    reports["logs-connectivity"] = {
        "agent": "logs-connectivity",
        "log_findings": [
            {
                "source": "web",
                "level": "error",
                "summary": f"Invented log line containing {SECRET_SENTINEL}",
            }
        ],
        "connectivity_findings": [],
        "runtime_failure_mode": "crash",
        "preliminary_assessment": f"Logs failed with {SECRET_SENTINEL}",
    }

    def complete(prompt_path: str, _payload: dict[str, Any]) -> str:
        agent = prompt_path.removeprefix("mas/").removesuffix(".md")
        if agent in reports:
            return json.dumps(reports[agent])
        return json.dumps(_valid_sparse_rca())

    service, store = _make_api_service(
        tmp_path,
        complete=complete,
        coverage_overrides={
            "metrics": SourceCoverage(status="unreachable", count=0, reason="prometheus down")
        },
        tracer=tracer,
    )
    try:
        with pytest.raises(ApiProviderUnavailableError):
            asyncio.run(
                service.execute_run(
                    request_id="req_redact_fail",
                    variant="mas",
                    target=TargetSelector(context_alias="lab", namespaces=["rca-test"]),
                    incident=f"Crash with {SECRET_SENTINEL}",
                )
            )
        persisted = service.list_runs(limit=5)[0]
    finally:
        service.shutdown()
        store.close()

    assert persisted.status == "failed"
    assert persisted.error_code == "invalid_model_output"
    assert any("metrics: unreachable" in lim for lim in persisted.limitations)
    assert any("mas/logs-connectivity: failed (unsupported_observation)" in lim for lim in persisted.limitations)
    serialized_run = persisted.model_dump_json()
    assert SECRET_SENTINEL not in serialized_run
    for record in tracer.records:
        assert SECRET_SENTINEL not in (record.error_message or "")
        assert "resources" not in (record.error_message or "")


def test_delayed_provider_timeout_and_cancellation_emit_single_terminal_status(
    tmp_path: Path,
) -> None:
    # 1. Delayed provider timeout on a specialist blocks coordinator and records timing.
    timeout_tracer = RecordingMasTracer()

    def slow_timeout_complete(prompt_path: str, _payload: dict[str, Any]) -> str:
        if prompt_path == "mas/metrics-telemetry.md":
            time.sleep(0.02)
            raise ProviderTimeoutError(f"e-infra timed out with {SECRET_SENTINEL}")
        agent = prompt_path.removeprefix("mas/").removesuffix(".md")
        if agent in _valid_sparse_reports():
            return json.dumps(_valid_sparse_reports()[agent])
        return json.dumps(_valid_sparse_rca())

    svc_timeout, store_timeout = _make_api_service(
        tmp_path,
        complete=slow_timeout_complete,
        coverage_overrides={
            "logs": SourceCoverage(status="forbidden", count=0, reason="rbac denied")
        },
        tracer=timeout_tracer,
    )
    try:
        with pytest.raises(ApiTimeoutError):
            asyncio.run(
                svc_timeout.execute_run(
                    request_id="req_timeout_mas",
                    variant="mas",
                    target=TargetSelector(context_alias="lab", namespaces=["rca-test"]),
                    incident="Timeout test",
                )
            )
        timeout_run = svc_timeout.list_runs(limit=5)[0]
    finally:
        svc_timeout.shutdown()
        store_timeout.close()

    assert timeout_run.status == "failed"
    assert timeout_run.error_code == "timeout"
    assert any("logs: forbidden" in lim for lim in timeout_run.limitations)
    assert any("mas/metrics-telemetry: failed (provider_timeout)" in lim for lim in timeout_run.limitations)
    metrics_trace = next(r for r in timeout_tracer.records if r.agent == "metrics-telemetry")
    assert metrics_trace.status == "failed"
    assert metrics_trace.error_category == "provider_timeout"
    assert metrics_trace.duration_seconds >= 0.015
    assert SECRET_SENTINEL not in (metrics_trace.error_message or "")
    assert any(
        r.agent == "coordinator" and r.status == "blocked"
        for r in timeout_tracer.records
    )

    # 2. Mid-run cancellation stops coordinator and emits one interrupted terminal RunResult.
    cancel_event = threading.Event()
    cancel_tracer = RecordingMasTracer()
    called: list[str] = []

    def cancelling_complete(prompt_path: str, _payload: dict[str, Any]) -> str:
        called.append(prompt_path)
        cancel_event.set()
        agent = prompt_path.removeprefix("mas/").removesuffix(".md")
        return json.dumps(_valid_sparse_reports()[agent])

    svc_cancel, store_cancel = _make_api_service(
        tmp_path,
        complete=cancelling_complete,
        tracer=cancel_tracer,
    )
    try:
        cancelled_run = asyncio.run(
            svc_cancel.execute_run(
                request_id="req_cancel_mas",
                variant="mas",
                target=TargetSelector(context_alias="lab", namespaces=["rca-test"]),
                incident="Cancel test",
                cancel_event=cancel_event,
            )
        )
    finally:
        svc_cancel.shutdown()
        store_cancel.close()

    assert cancelled_run.status == "interrupted"
    assert cancelled_run.error_code == "client_disconnected"
    assert "mas/coordinator.md" not in called
    assert any(r.status == "cancelled" and r.error_category == "cancelled" for r in cancel_tracer.records)
    assert any(r.agent == "coordinator" and r.status == "blocked" for r in cancel_tracer.records)


def test_build_workflow_mas_with_validate_evidence_flag_preserves_invoke_contract() -> None:
    reports = _valid_sparse_reports()
    rca = _valid_sparse_rca()

    def complete(prompt_path: str, _payload: dict[str, Any]) -> str:
        agent = prompt_path.removeprefix("mas/").removesuffix(".md")
        if agent in reports:
            return json.dumps(reports[agent])
        return json.dumps(rca)

    tracer = RecordingMasTracer()
    graph = build_workflow("mas", complete=complete, validate_evidence=True, tracer=tracer)
    result = graph.invoke({"context": _sparse_context()})
    assert result["root-cause"] == rca
    assert [r.status for r in tracer.records] == ["passed", "passed", "passed", "passed"]
