"""ASGI and OpenAI SDK tests for the RCAnnon FastAPI service."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from openai import OpenAI

from rca.api import ApiConfig, RunStore, create_app
from rca.contracts.provider import ProviderAuthError, ProviderTimeoutError
from rca.contracts.runtime import (
    EvidenceSnapshot,
    RunResult,
    SourceCoverage,
    TargetSelector,
    compute_scope_hash,
    derive_default_coverage,
    utc_now_iso,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCENARIO_01 = json.loads((REPO_ROOT / "scenarios/01/rca.json").read_text(encoding="utf-8"))

UI_TOKEN = "ui-token-abc123456"
OP_TOKEN = "op-token-xyz987654"
SECRET_CANARY = "sk-secret-canary-do-not-leak-123456"

MAS_REPORTS = {
    "k8s-config-events": {
        "agent": "k8s-config-events",
        "anomalies": [
            {
                "resource": "Deployment/api",
                "field": "spec.template.spec.containers[0].resources.limits.memory",
                "issue": "Memory limit 64Mi is below the expected 128Mi allocation.",
                "severity": "high",
            }
        ],
        "suspicious_resources": ["Deployment/api"],
        "significant_events": [],
        "preliminary_assessment": "64Mi limit appears insufficient.",
    },
    "logs-connectivity": {
        "agent": "logs-connectivity",
        "log_findings": [],
        "connectivity_findings": [],
        "runtime_failure_mode": "unknown",
        "preliminary_assessment": "No runtime logs or checks were provided.",
    },
    "metrics-telemetry": {
        "agent": "metrics-telemetry",
        "resource_findings": [],
        "throttling_detected": False,
        "saturation_identified": None,
        "preliminary_assessment": "No runtime metrics were provided.",
    },
}

VALID_RCA = {
    "cause": "Deployment/api limits container web to 64Mi while it allocates 128Mi.",
    "causal_chain": "The container exceeds its 64Mi cgroup memory limit and is OOMKilled.",
    "supporting_evidence": [
        "Deployment/api spec.template.spec.containers[0].resources.limits.memory=64Mi",
        "ALLOC_MIB=128 configured in Deployment/api env",
    ],
    "remediation": "Increase container memory limit above 128Mi.",
    "recovery_check": "Pods remain Ready with zero OOMKilled restarts.",
}


def _make_config(tmp_path: Path, **overrides: Any) -> ApiConfig:
    defaults: dict[str, Any] = {
        "ui_tokens": frozenset({UI_TOKEN}),
        "operator_tokens": frozenset({OP_TOKEN}),
        "allowed_scopes": {
            "lab": frozenset({"rca-01", "demo"}),
        },
        "db_path": tmp_path / "api.db",
        "snapshot_ttl_seconds": 300,
        "max_snapshots": 50,
        "max_runs": 100,
        "max_bundle_bytes": 64 * 1024,
        "max_prompt_chars": 2000,
        "max_workers": 2,
        "run_deadline_seconds": 5.0,
        "extra_redaction_secrets": (SECRET_CANARY,),
    }
    defaults.update(overrides)
    return ApiConfig(**defaults)


def _canned_complete(prompt_path: str, _payload: dict[str, Any]) -> str:
    if prompt_path == "base.md":
        return "Deployment/api has a 64Mi memory limit below its 128Mi working set."
    if prompt_path == "optimal.md" or prompt_path == "mas/coordinator.md":
        return json.dumps(VALID_RCA)
    agent = prompt_path.removeprefix("mas/").removesuffix(".md")
    if agent in MAS_REPORTS:
        return json.dumps(MAS_REPORTS[agent])
    raise AssertionError(f"Unexpected prompt {prompt_path}")


def _scenario_collector(
    target: TargetSelector,
    incident: str,
    *,
    degraded: bool = False,
) -> EvidenceSnapshot:
    context = json.loads(json.dumps(SCENARIO_01["context"]))
    context["incident"] = incident
    coverage = derive_default_coverage(context)
    if degraded:
        coverage["metrics"] = SourceCoverage(
            status="unreachable", count=0, reason="prometheus unavailable"
        )
        coverage["logs"] = SourceCoverage(status="forbidden", count=0, reason="rbac denied")
    return EvidenceSnapshot(
        snapshot_id="snap_collected_01",
        scope_hash=compute_scope_hash(target.context_alias, target.namespaces, context),
        source_type="live",
        context_alias=target.context_alias,
        namespaces=list(target.namespaces),
        collected_at=utc_now_iso(),
        observed_at=utc_now_iso(),
        context=context,
        coverage=coverage,
    )


def _ui_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {UI_TOKEN}"}
    if extra:
        headers.update(extra)
    return headers


def _op_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {OP_TOKEN}"}
    if extra:
        headers.update(extra)
    return headers


def test_api_config_from_env_and_token_separation(tmp_path: Path) -> None:
    ui_file = tmp_path / "ui.key"
    ui_file.write_text(f"{UI_TOKEN}\n", encoding="utf-8")
    op_file = tmp_path / "op.key"
    op_file.write_text(f"{OP_TOKEN}\n", encoding="utf-8")

    cfg = ApiConfig.from_env(
        {
            "RCANNON_UI_BEARER_TOKEN_FILE": str(ui_file),
            "RCANNON_OPERATOR_BEARER_TOKEN_FILE": str(op_file),
            "RCANNON_ALLOWED_SCOPES": '{"lab": ["rca-01", "demo"]}',
            "RCANNON_DB_PATH": str(tmp_path / "env.db"),
            "RCANNON_SNAPSHOT_TTL_SECONDS": "120",
        }
    )
    assert cfg.is_scope_allowed("lab", ["rca-01"])
    assert not cfg.is_scope_allowed("prod", ["rca-01"])
    assert not cfg.is_scope_allowed("lab", ["kube-system"])
    assert UI_TOKEN not in repr(cfg)
    assert OP_TOKEN not in repr(cfg)

    with pytest.raises(ValueError, match="distinct"):
        ApiConfig(
            ui_tokens=frozenset({"shared-token"}),
            operator_tokens=frozenset({"shared-token"}),
        )


def test_healthz_and_readyz_stay_healthy_when_collector_fails(tmp_path: Path) -> None:
    def failing_collector(_target: TargetSelector, _incident: str) -> EvidenceSnapshot:
        raise ConnectionError("kubernetes api down")

    app = create_app(
        config=_make_config(tmp_path),
        complete=_canned_complete,
        collector=failing_collector,
    )
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        ready = client.get("/readyz")
        assert ready.status_code == 200
        assert ready.json()["status"] == "ready"
        assert set(ready.json()["variants"]) >= {"base", "optimal", "mas"}


def test_authentication_and_cross_token_isolation(tmp_path: Path) -> None:
    app = create_app(
        config=_make_config(tmp_path),
        complete=_canned_complete,
        collector=_scenario_collector,
    )
    with TestClient(app) as client:
        # Missing or wrong Bearer token -> 401
        assert client.get("/v1/models").status_code == 401
        assert client.get("/v1/models", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.get("/v1/models", headers={"Authorization": "Basic abc"}).status_code == 401
        assert client.get("/api/v1/runs").status_code == 401

        # Cross-token rejection -> 403
        assert client.get("/v1/models", headers=_op_headers()).status_code == 403
        assert (
            client.post(
                "/v1/chat/completions",
                headers=_op_headers(),
                json={
                    "model": "rcannon-base",
                    "messages": [{"role": "user", "content": "/rca context=lab namespace=demo -- fail"}],
                },
            ).status_code
            == 403
        )
        assert client.get("/api/v1/runs", headers=_ui_headers()).status_code == 403
        assert (
            client.post(
                "/api/v1/snapshots",
                headers=_ui_headers(),
                json={"target": {"context_alias": "lab", "namespaces": ["demo"]}, "incident": "x"},
            ).status_code
            == 403
        )


def test_scope_validation_tool_overrides_and_payload_size_rejected(tmp_path: Path) -> None:
    app = create_app(
        config=_make_config(tmp_path, max_bundle_bytes=1024),
        complete=_canned_complete,
        collector=_scenario_collector,
    )
    with TestClient(app) as client:
        # Unapproved alias / namespace -> 403
        res = client.post(
            "/v1/chat/completions",
            headers=_ui_headers(),
            json={
                "model": "rcannon-mas",
                "messages": [
                    {"role": "user", "content": "/rca context=prod namespace=rca-01 -- outage"}
                ],
            },
        )
        assert res.status_code == 403

        res_ns = client.post(
            "/v1/chat/completions",
            headers=_ui_headers(),
            json={
                "model": "rcannon-mas",
                "messages": [
                    {"role": "user", "content": "/rca context=lab namespace=kube-system -- outage"}
                ],
            },
        )
        assert res_ns.status_code == 403

        # Unscoped prompt or forbidden directive options -> 422
        for bad_content in (
            "Why is checkout failing?",
            "/rca host=https://evil.invalid context=lab namespace=demo -- outage",
            "/rca context=lab namespace=demo",
            "/rca snapshot=bad$id!",
        ):
            bad_res = client.post(
                "/v1/chat/completions",
                headers=_ui_headers(),
                json={"model": "rcannon-base", "messages": [{"role": "user", "content": bad_content}]},
            )
            assert bad_res.status_code == 422

        # Tools / temperature / attachment list content overrides -> 422 without leaking secret
        override_res = client.post(
            "/v1/chat/completions",
            headers=_ui_headers(),
            json={
                "model": "rcannon-optimal",
                "temperature": 0.9,
                "tools": [{"type": "function", "function": {"name": "exec"}}],
                "messages": [{"role": "user", "content": f"/rca context=lab namespace=demo -- {SECRET_CANARY}"}],
            },
        )
        assert override_res.status_code == 422
        assert SECRET_CANARY not in override_res.text

        attach_res = client.post(
            "/v1/chat/completions",
            headers=_ui_headers(),
            json={
                "model": "rcannon-base",
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "/rca context=lab namespace=demo -- hi"}]}
                ],
            },
        )
        assert attach_res.status_code == 422

        # Malformed bundle -> 422
        bad_bundle = client.post(
            "/api/v1/snapshots",
            headers=_op_headers(),
            json={"bundle": {"context": {"incident": "missing arrays"}}},
        )
        assert bad_bundle.status_code == 422

        # Oversized bundle exceeding max_bundle_bytes (1024) -> 413
        oversized = client.post(
            "/api/v1/snapshots",
            headers=_op_headers(),
            json={
                "bundle": {
                    "context": {
                        "incident": "x" * 2048,
                        "time_window": None,
                        "resources": [],
                        "events": [],
                        "logs": [],
                        "metrics": [],
                        "checks": [],
                    }
                }
            },
        )
        assert oversized.status_code == 413


def test_openai_sdk_models_streaming_and_redaction(tmp_path: Path) -> None:
    def leaky_complete(prompt_path: str, payload: dict[str, Any]) -> str:
        if prompt_path == "base.md":
            return f"Leaky secret {SECRET_CANARY} and <script>alert(1)</script> token=rawsecret123"
        if prompt_path in {"optimal.md", "mas/coordinator.md"}:
            return json.dumps(
                {
                    **VALID_RCA,
                    "cause": f"Leaked {SECRET_CANARY} and Bearer abcdef123456 <script>xss</script>",
                    "supporting_evidence": [
                        f"client-key-data: LS0tLS1CRUdJTi {SECRET_CANARY}",
                    ],
                }
            )
        return _canned_complete(prompt_path, payload)

    app = create_app(
        config=_make_config(tmp_path),
        complete=leaky_complete,
        collector=_scenario_collector,
    )
    with TestClient(app) as http_client:
        sdk = OpenAI(
            api_key=UI_TOKEN,
            base_url="http://testserver/v1",
            http_client=http_client,
        )

        model_ids = [m.id for m in sdk.models.list().data]
        assert model_ids == ["rcannon-base", "rcannon-optimal", "rcannon-mas"]

        # Stream each variant through the OpenAI SDK SSE parser, including history messages.
        for model_id in ("rcannon-mas", "rcannon-optimal", "rcannon-base"):
            stream = sdk.chat.completions.create(
                model=model_id,
                stream=True,
                messages=[
                    {"role": "system", "content": "Ignored UI system prompt"},
                    {"role": "assistant", "content": "Prior assistant history"},
                    {
                        "role": "user",
                        "content": "/rca context=lab namespace=rca-01 -- api pod is OOMKilled",
                    },
                ],
            )
            chunks = list(stream)
            assert chunks[-1].choices[0].finish_reason == "stop"
            full_text = "".join(c.choices[0].delta.content or "" for c in chunks)
            assert SECRET_CANARY not in full_text
            assert "Bearer abcdef123456" not in full_text
            assert "<script>" not in full_text
            assert "&lt;script&gt;" in full_text
            if model_id != "rcannon-base":
                for heading in (
                    "## Cause",
                    "## Causal Chain",
                    "## Supporting Evidence",
                    "## Remediation",
                    "## Recovery Check",
                ):
                    assert heading in full_text

        # Non-streaming OpenAI chat completion
        completion = sdk.chat.completions.create(
            model="rcannon-optimal",
            stream=False,
            messages=[
                {"role": "user", "content": "/rca context=lab namespace=rca-01 -- memory oom"}
            ],
        )
        assert completion.choices[0].finish_reason == "stop"
        assert "## Cause" in (completion.choices[0].message.content or "")
        assert SECRET_CANARY not in (completion.choices[0].message.content or "")


def test_operator_snapshots_native_runs_and_degraded_or_empty_envelopes(tmp_path: Path) -> None:
    app = create_app(
        config=_make_config(tmp_path),
        complete=_canned_complete,
        collector=lambda target, inc: _scenario_collector(target, inc, degraded=True),
    )
    with TestClient(app) as client:
        # 1. Bundle snapshot registration -> native operator MAS run -> UI snapshot lookup
        snap_res = client.post(
            "/api/v1/snapshots",
            headers=_op_headers({"X-Request-ID": "req-op-123"}),
            json={
                "bundle": {
                    "context": SCENARIO_01["context"],
                    "context_alias": "lab",
                    "namespaces": ["rca-01"],
                }
            },
        )
        assert snap_res.status_code == 201
        assert snap_res.headers["X-Request-ID"] == "req-op-123"
        snap_id = snap_res.json()["snapshot_id"]

        snap_get = client.get(f"/api/v1/snapshots/{snap_id}", headers=_op_headers())
        assert snap_get.status_code == 200
        assert snap_get.json()["has_usable_evidence"] is True

        run_res = client.post(
            "/api/v1/runs",
            headers=_op_headers(),
            json={"variant": "mas", "snapshot_id": snap_id},
        )
        assert run_res.status_code == 200
        run_data = run_res.json()
        assert run_data["status"] == "diagnosed"
        assert run_data["result"] == VALID_RCA
        assert run_data["usage"]["logical_calls"] == 4
        assert run_res.headers["X-Run-ID"] == run_data["run_id"]

        # UI can query the registered snapshot_id via /rca snapshot=...
        ui_snap_res = client.post(
            "/v1/chat/completions",
            headers=_ui_headers(),
            json={
                "model": "rcannon-optimal",
                "messages": [{"role": "user", "content": f"/rca snapshot={snap_id}"}],
            },
        )
        assert ui_snap_res.status_code == 200
        assert "## Cause" in ui_snap_res.json()["choices"][0]["message"]["content"]

        # 2. Degraded live collection (metrics unreachable, logs forbidden) -> qualified status
        degraded_res = client.post(
            "/api/v1/runs",
            headers=_op_headers(),
            json={
                "variant": "mas",
                "target": {"context_alias": "lab", "namespaces": ["rca-01"]},
                "incident": "Pod restarts",
            },
        )
        assert degraded_res.status_code == 200
        degraded_data = degraded_res.json()
        assert degraded_data["status"] == "qualified"
        assert any("metrics: unreachable" in lim for lim in degraded_data["limitations"])
        assert any("logs: forbidden" in lim for lim in degraded_data["limitations"])

        # Run lookup & list
        fetched = client.get(f"/api/v1/runs/{degraded_data['run_id']}", headers=_op_headers())
        assert fetched.status_code == 200
        assert fetched.json()["run_id"] == degraded_data["run_id"]
        listed = client.get("/api/v1/runs?limit=5", headers=_op_headers())
        assert listed.status_code == 200
        assert len(listed.json()["runs"]) == 3


def test_insufficient_evidence_never_calls_model(tmp_path: Path) -> None:
    calls = 0

    def tracked_complete(prompt_path: str, payload: dict[str, Any]) -> str:
        nonlocal calls
        calls += 1
        return _canned_complete(prompt_path, payload)

    def unreachable_collector(_target: TargetSelector, _incident: str) -> EvidenceSnapshot:
        raise ConnectionError("cluster unreachable")

    app = create_app(
        config=_make_config(tmp_path),
        complete=tracked_complete,
        collector=unreachable_collector,
    )
    with TestClient(app) as client:
        op_res = client.post(
            "/api/v1/runs",
            headers=_op_headers(),
            json={
                "variant": "mas",
                "target": {"context_alias": "lab", "namespaces": ["demo"]},
                "incident": "Everything unreachable",
            },
        )
        assert op_res.status_code == 200
        body = op_res.json()
        assert body["status"] == "insufficient_evidence"
        assert body["result"] is None
        assert body["usage"]["logical_calls"] == 0

        ui_res = client.post(
            "/v1/chat/completions",
            headers=_ui_headers(),
            json={
                "model": "rcannon-mas",
                "messages": [
                    {"role": "user", "content": "/rca context=lab namespace=demo -- outage"}
                ],
            },
        )
        assert ui_res.status_code == 200
        assert "## Insufficient Evidence" in ui_res.json()["choices"][0]["message"]["content"]
        assert calls == 0


def test_provider_errors_timeout_cancellation_and_restart_recovery(tmp_path: Path) -> None:
    db_file = tmp_path / "errors.db"

    # 503 on provider auth error -> secrets scrubbed, HTTP 503
    def auth_fail(_prompt_path: str, _payload: dict[str, Any]) -> str:
        raise ProviderAuthError(f"auth failed with {SECRET_CANARY}")

    app_auth = create_app(
        config=_make_config(tmp_path, db_path=db_file),
        complete=auth_fail,
        collector=_scenario_collector,
    )
    with TestClient(app_auth) as client:
        res_503 = client.post(
            "/v1/chat/completions",
            headers=_ui_headers(),
            json={
                "model": "rcannon-optimal",
                "messages": [{"role": "user", "content": "/rca context=lab namespace=rca-01 -- oom"}],
            },
        )
        assert res_503.status_code == 503
        assert SECRET_CANARY not in res_503.text

    # 503 on invalid structured output -> HTTP 503
    app_bad_json = create_app(
        config=_make_config(tmp_path, db_path=db_file),
        complete=lambda _p, _payload: "not valid json",
        collector=_scenario_collector,
    )
    with TestClient(app_bad_json) as client:
        res_bad = client.post(
            "/api/v1/runs",
            headers=_op_headers(),
            json={
                "variant": "optimal",
                "target": {"context_alias": "lab", "namespaces": ["rca-01"]},
                "incident": "oom",
            },
        )
        assert res_bad.status_code == 503

    # 504 on timeout -> HTTP 504
    def slow_fail(_prompt_path: str, _payload: dict[str, Any]) -> str:
        raise ProviderTimeoutError("timed out")

    app_timeout = create_app(
        config=_make_config(tmp_path, db_path=db_file),
        complete=slow_fail,
        collector=_scenario_collector,
    )
    with TestClient(app_timeout) as client:
        res_504 = client.post(
            "/api/v1/runs",
            headers=_op_headers(),
            json={
                "variant": "base",
                "target": {"context_alias": "lab", "namespaces": ["rca-01"]},
                "incident": "oom",
            },
        )
        assert res_504.status_code == 504

    # Forced mid-run cancellation -> persists status='interrupted' and stops remaining calls
    cancel_event = threading.Event()
    called_prompts: list[str] = []

    def cancelling_complete(prompt_path: str, payload: dict[str, Any]) -> str:
        called_prompts.append(prompt_path)
        cancel_event.set()
        return _canned_complete(prompt_path, payload)

    store_cancel = RunStore(db_file)
    store_cancel.initialize()
    try:
        from rca.api.service import RcaApiService

        svc = RcaApiService(
            config=_make_config(tmp_path, db_path=db_file),
            store=store_cancel,
            complete=cancelling_complete,
            collector=_scenario_collector,
        )
        cancelled_run = asyncio.run(
            svc.execute_run(
                request_id="req_cancel",
                variant="mas",
                target=TargetSelector(context_alias="lab", namespaces=["rca-01"]),
                incident="oom",
                cancel_event=cancel_event,
            )
        )
        svc.shutdown()
        assert cancelled_run.status == "interrupted"
        assert cancelled_run.error_code == "client_disconnected"
        assert "mas/coordinator.md" not in called_prompts

        # Simulate process restart with one completed run and one leftover 'running' run
        completed_run = RunResult(
            request_id="req_ok",
            run_id="run_ok_restart",
            variant="base",
            status="diagnosed",
            result="safe ok",
            created_at=utc_now_iso(),
            completed_at=utc_now_iso(),
        )
        inflight_run = RunResult(
            request_id="req_hang",
            run_id="run_hang_restart",
            variant="mas",
            status="running",
            created_at=utc_now_iso(),
        )
        store_cancel.put_run(completed_run, max_runs=100)
        store_cancel.put_run(inflight_run, max_runs=100)
    finally:
        store_cancel.close()

    # Re-create app on the same SQLite DB file and verify recovery over API
    app_restarted = create_app(
        config=_make_config(tmp_path, db_path=db_file),
        complete=_canned_complete,
        collector=_scenario_collector,
    )
    with TestClient(app_restarted) as client:
        done_res = client.get("/api/v1/runs/run_ok_restart", headers=_op_headers())
        assert done_res.status_code == 200
        assert done_res.json()["status"] == "diagnosed"

        hang_res = client.get("/api/v1/runs/run_hang_restart", headers=_op_headers())
        assert hang_res.status_code == 200
        assert hang_res.json()["status"] == "interrupted"
        assert hang_res.json()["error_code"] == "process_restarted"
        # Ensure secret canary never leaked into persisted SQLite DB either
        assert SECRET_CANARY not in db_file.read_text(encoding="utf-8", errors="ignore")
