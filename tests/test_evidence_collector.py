"""Tests for read-only Kubernetes evidence collector, isolation, and adapters."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import httpx
import kubernetes.client as k8s_client
import pytest
import yaml
from jsonschema import Draft202012Validator

from rca.api.config import ApiConfig
from rca.api.service import RcaApiService
from rca.api.storage import RunStore
from rca.contracts.runtime import CANONICAL_EVIDENCE_SOURCES, TargetSelector
from rca.contracts.schemas import CASE
from rca.evidence import (
    AdapterForbiddenError,
    AdapterInvalidPayloadError,
    AdapterNotFoundError,
    AdapterRedirectError,
    AdapterResourceExpiredError,
    AdapterTimeoutError,
    AdapterUnreachableError,
    CollectionLimits,
    DiscoveryInfo,
    ForbiddenOperationError,
    HttpPrometheusAdapter,
    KubeconfigSecurityError,
    KubernetesClientAdapter,
    KubernetesEvidenceCollector,
    PageResult,
    ScopeDeniedError,
    SnapshotCache,
    TargetClusterConfig,
    assert_resource_allowed,
    build_collector_fn,
    collect,
    load_isolated_kube_configuration,
)
from rca.workflows import build_workflow

REPO_ROOT = Path(__file__).resolve().parent.parent
RBAC_MANIFEST = REPO_ROOT / "infrastructure" / "k8s" / "rbac" / "rcannon-readonly.yaml"
CONTEXT_VALIDATOR = Draft202012Validator(CASE["properties"]["context"])


class FakeKubernetesAdapter:
    """Deterministic programmable mock of KubernetesAdapter."""

    def __init__(
        self,
        *,
        discovery: DiscoveryInfo | Exception | None = None,
        pages: dict[tuple[str, str, str | None, str | None], Any] | None = None,
        logs: dict[tuple[str, str, str, bool], str | Exception] | None = None,
        delay_seconds: float = 0.0,
    ) -> None:
        self._discovery = discovery or DiscoveryInfo(
            api_groups={"", "apps", "discovery.k8s.io", "networking.k8s.io", "storage.k8s.io", "rbac.authorization.k8s.io", "events.k8s.io", "metrics.k8s.io"},
            group_resources={
                "": {"pods", "services", "events", "configmaps", "persistentvolumeclaims", "persistentvolumes", "serviceaccounts", "endpoints", "nodes"},
                "apps": {"deployments", "replicasets", "daemonsets", "statefulsets"},
                "discovery.k8s.io": {"endpointslices"},
                "networking.k8s.io": {"networkpolicies", "ingresses"},
                "storage.k8s.io": {"storageclasses", "csidrivers", "csinodes"},
                "rbac.authorization.k8s.io": {"roles", "rolebindings", "clusterroles", "clusterrolebindings"},
                "events.k8s.io": {"events"},
                "metrics.k8s.io": {"pods", "nodes"},
            },
        )
        self._pages = pages or {}
        self._logs = logs or {}
        self._delay_seconds = delay_seconds
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def discover(self, *, timeout_seconds: float) -> DiscoveryInfo:
        self.calls.append(("discover", (timeout_seconds,)))
        if self._delay_seconds:
            time.sleep(self._delay_seconds)
        if isinstance(self._discovery, Exception):
            raise self._discovery
        return self._discovery

    def list_resource(
        self,
        group: str,
        version: str,
        plural: str,
        *,
        namespace: str | None,
        label_selector: str | None,
        limit: int,
        continue_token: str | None,
        timeout_seconds: float,
    ) -> PageResult:
        assert_resource_allowed(group, version, plural)
        self.calls.append(("list", (group, version, plural, namespace, continue_token)))
        if self._delay_seconds:
            time.sleep(self._delay_seconds)
        key = (group, plural, namespace, continue_token)
        outcome = self._pages.get(key)
        if outcome is None:
            fallback = self._pages.get((group, plural, namespace, None))
            if callable(fallback):
                outcome = fallback
        if callable(outcome):
            outcome = outcome(continue_token)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is None:
            return PageResult(items=[], continue_token=None)
        return outcome

    def read_pod_log(
        self,
        namespace: str,
        pod_name: str,
        container: str,
        *,
        previous: bool,
        tail_lines: int,
        limit_bytes: int,
        timeout_seconds: float,
    ) -> str:
        self.calls.append(("log", (namespace, pod_name, container, previous)))
        outcome = self._logs.get((namespace, pod_name, container, previous))
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is None:
            raise AdapterNotFoundError("Container log unavailable.")
        return outcome


def _deployment_checkout() -> dict[str, Any]:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "checkout", "namespace": "demo"},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "web",
                            "image": "python:3.12.8-slim",
                            "resources": {
                                "requests": {"cpu": "25m", "memory": "32Mi"},
                                "limits": {"cpu": "500m", "memory": "64Mi"},
                            },
                            "env": [
                                {"name": "AUTH_TOKEN", "value": "canary-env-token-123"},
                                {
                                    "name": "DB_PASS",
                                    "valueFrom": {
                                        "secretKeyRef": {
                                            "name": "db-secret",
                                            "key": "password",
                                            "optional": False,
                                        }
                                    },
                                },
                            ],
                        }
                    ]
                }
            }
        },
    }


def _pod_checkout(*, restarts: int = 1) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "checkout-abc", "namespace": "demo"},
        "spec": {"containers": [{"name": "web", "image": "python:3.12.8-slim"}]},
        "status": {
            "phase": "Running",
            "containerStatuses": [{"name": "web", "ready": False, "restartCount": restarts}],
        },
    }


def test_kubeconfig_loader_isolates_contexts_and_rejects_unsafe_configs(tmp_path: Path):
    default_before = k8s_client.Configuration.get_default_copy().host
    kubeconfig = tmp_path / "kubeconfig.yaml"
    kubeconfig.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "v1",
                "kind": "Config",
                "current-context": "other",
                "clusters": [
                    {
                        "name": "lab-cluster",
                        "cluster": {
                            "server": "https://lab.example.internal:6443",
                            "certificate-authority-data": "LS0tLS1CRUdJTiBDRVJURS0tLS0t",
                        },
                    },
                    {
                        "name": "insecure-cluster",
                        "cluster": {
                            "server": "https://insecure.example.internal:6443",
                            "insecure-skip-tls-verify": True,
                        },
                    },
                ],
                "users": [
                    {"name": "lab-user", "user": {"token": "canary-kube-bearer-xyz"}},
                    {"name": "exec-user", "user": {"exec": {"command": "/bin/sh"}}},
                    {"name": "auth-prov-user", "user": {"auth-provider": {"name": "oidc"}}},
                ],
                "contexts": [
                    {"name": "lab", "context": {"cluster": "lab-cluster", "user": "lab-user"}},
                    {"name": "exec-ctx", "context": {"cluster": "lab-cluster", "user": "exec-user"}},
                    {
                        "name": "ap-ctx",
                        "context": {"cluster": "lab-cluster", "user": "auth-prov-user"},
                    },
                    {
                        "name": "insecure-ctx",
                        "context": {"cluster": "insecure-cluster", "user": "lab-user"},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    cfg, secrets = load_isolated_kube_configuration(kubeconfig, "lab")
    assert cfg.host == "https://lab.example.internal:6443"
    assert cfg.verify_ssl is True
    assert cfg.api_key == {"authorization": "canary-kube-bearer-xyz"}
    assert "canary-kube-bearer-xyz" in secrets
    assert k8s_client.Configuration.get_default_copy().host == default_before

    for bad_ctx in ("exec-ctx", "ap-ctx", "insecure-ctx", "missing"):
        with pytest.raises(KubeconfigSecurityError):
            load_isolated_kube_configuration(kubeconfig, bad_ctx)


def test_readonly_allowlist_and_rbac_manifest_exclude_secrets_exec_and_writes():
    with pytest.raises(ForbiddenOperationError):
        assert_resource_allowed("", "v1", "secrets")
    with pytest.raises(ForbiddenOperationError):
        assert_resource_allowed("", "v1", "pods/exec")

    docs = list(yaml.safe_load_all(RBAC_MANIFEST.read_text(encoding="utf-8")))
    roles = [d for d in docs if isinstance(d, Mapping) and d.get("kind") in {"Role", "ClusterRole"}]
    assert len(roles) == 2
    for role in roles:
        for rule in role["rules"]:
            assert set(rule["verbs"]).issubset({"get", "list"})
            for res in rule["resources"]:
                assert "secrets" not in res
                assert "exec" not in res
                assert "proxy" not in res
                assert res != "*"


def test_full_collection_preserves_canonical_schema_redacts_secrets_and_feeds_mas():
    adapter = FakeKubernetesAdapter(
        pages={
            ("", "pods", "demo", None): PageResult(items=[_pod_checkout(restarts=2)]),
            ("apps", "deployments", "demo", None): PageResult(items=[_deployment_checkout()]),
            ("", "services", "demo", None): PageResult(
                items=[
                    {
                        "apiVersion": "v1",
                        "kind": "Service",
                        "metadata": {"name": "checkout", "namespace": "demo"},
                        "spec": {"ports": [{"port": 80}]},
                    }
                ]
            ),
            ("discovery.k8s.io", "endpointslices", "demo", None): PageResult(
                items=[
                    {
                        "apiVersion": "discovery.k8s.io/v1",
                        "kind": "EndpointSlice",
                        "metadata": {
                            "name": "checkout-abc",
                            "namespace": "demo",
                            "labels": {"kubernetes.io/service-name": "checkout"},
                        },
                        "endpoints": [{"addresses": ["10.0.0.5"], "conditions": {"ready": True}}],
                    }
                ]
            ),
            ("", "persistentvolumeclaims", "demo", None): PageResult(
                items=[
                    {
                        "apiVersion": "v1",
                        "kind": "PersistentVolumeClaim",
                        "metadata": {"name": "data", "namespace": "demo"},
                        "spec": {"storageClassName": "missing-sc"},
                    }
                ]
            ),
            ("discovery.k8s.io", "endpointslices", "kube-system", None): PageResult(
                items=[
                    {
                        "apiVersion": "discovery.k8s.io/v1",
                        "kind": "EndpointSlice",
                        "metadata": {
                            "name": "coredns-1",
                            "namespace": "kube-system",
                            "labels": {"kubernetes.io/service-name": "kube-dns"},
                        },
                        "endpoints": [{"addresses": ["10.96.0.10"], "conditions": {"ready": True}}],
                    }
                ]
            ),
            ("events.k8s.io", "events", "demo", None): PageResult(
                items=[
                    {
                        "metadata": {"namespace": "demo", "creationTimestamp": "2026-11-20T12:00:00Z"},
                        "reason": "OOMKilled",
                        "type": "Warning",
                        "regarding": {"kind": "Pod", "name": "checkout-abc"},
                        "note": "Exceeded 64Mi memory limit",
                    },
                    {
                        "metadata": {"namespace": "demo", "creationTimestamp": "2026-11-20T12:00:00Z"},
                        "reason": "OOMKilled",
                        "type": "Warning",
                        "regarding": {"kind": "Pod", "name": "checkout-abc"},
                        "note": "Exceeded 64Mi memory limit",
                    },
                ]
            ),
            ("metrics.k8s.io", "pods", "demo", None): PageResult(
                items=[
                    {
                        "metadata": {"name": "checkout-abc", "namespace": "demo"},
                        "timestamp": "2026-11-20T12:00:00Z",
                        "containers": [{"name": "web", "usage": {"memory": "62Mi"}}],
                    }
                ]
            ),
        },
        logs={
            ("demo", "checkout-abc", "web", True): "2026-11-20T11:59:59Z Authorization: Bearer secret-log-canary\nretained_mib=62",
            ("demo", "checkout-abc", "web", False): "2026-11-20T12:00:01Z listening",
        },
    )

    collector = KubernetesEvidenceCollector()
    collector.register_target(
        TargetClusterConfig(
            context_alias="lab",
            allowed_namespaces=frozenset({"demo"}),
            extra_secrets=("secret-log-canary",),
        ),
        k8s_adapter=adapter,
    )

    snap = collect(
        TargetSelector(context_alias="lab", namespaces=["demo"]),
        collector=collector,
        incident="Checkout fails with OOMKilled",
    )
    CONTEXT_VALIDATOR.validate(snap.context)
    assert snap.source_type == "live"
    assert snap.observed_at is not None
    assert all(snap.coverage[s].status == "observed" for s in CANONICAL_EVIDENCE_SOURCES)
    assert len(snap.context["events"]) == 1  # deduplicated
    assert len(snap.context["logs"]) == 2
    assert any(c["kind"] == "pvc_storageclass" and c["status"] == "failed" for c in snap.context["checks"])
    assert any(c["kind"] == "dns_config_state" and c["status"] == "passed" for c in snap.context["checks"])

    serialized = json.dumps(snap.model_dump())
    assert "secret-log-canary" not in serialized
    assert "canary-env-token-123" not in serialized

    # Verify sanitized Deployment feeds MAS metrics-telemetry resource_limits cleanly
    captured_limits: list[Any] = []

    def fake_complete(prompt_path: str, payload: Mapping[str, Any]) -> str:
        if prompt_path == "mas/metrics-telemetry.md":
            captured_limits.extend(payload["resource_limits"])
            return json.dumps(
                {
                    "agent": "metrics-telemetry",
                    "resource_findings": [],
                    "throttling_detected": False,
                    "saturation_identified": "memory",
                    "preliminary_assessment": "Memory near 64Mi limit.",
                }
            )
        if prompt_path == "mas/k8s-config-events.md":
            return json.dumps(
                {
                    "agent": "k8s-config-events",
                    "anomalies": [],
                    "suspicious_resources": ["Deployment/checkout"],
                    "significant_events": [],
                    "preliminary_assessment": "OOMKilled observed.",
                }
            )
        if prompt_path == "mas/logs-connectivity.md":
            return json.dumps(
                {
                    "agent": "logs-connectivity",
                    "log_findings": [],
                    "connectivity_findings": [],
                    "runtime_failure_mode": "oom",
                    "preliminary_assessment": "Previous container OOMKilled.",
                }
            )
        return json.dumps(
            {
                "cause": "64Mi limit too small",
                "causal_chain": "Working set exceeds 64Mi.",
                "supporting_evidence": ["Deployment/checkout limit 64Mi"],
                "remediation": "Raise memory limit.",
                "recovery_check": "Pods stay Ready.",
            }
        )

    out = build_workflow("mas", complete=fake_complete).invoke({"context": snap.context})
    assert out["root-cause"]["cause"] == "64Mi limit too small"
    assert captured_limits == [
        {
            "resource": "Deployment/checkout",
            "namespace": "demo",
            "container": "web",
            "requests": {"cpu": "25m", "memory": "32Mi"},
            "limits": {"cpu": "500m", "memory": "64Mi"},
        }
    ]


def test_discovery_fallback_pagination_410_and_partial_rbac_errors():
    page_calls = {"count": 0}

    def pod_pages(token: str | None) -> PageResult:
        page_calls["count"] += 1
        if token is None:
            if page_calls["count"] == 1:
                return PageResult(items=[_pod_checkout(restarts=0)], continue_token="tok-1")
            return PageResult(items=[_pod_checkout(restarts=0)], continue_token="tok-2")
        if token == "tok-1":
            raise AdapterResourceExpiredError("410 Gone")
        if token == "tok-2":
            return PageResult(items=[_pod_checkout(restarts=0)], continue_token="tok-3")
        return PageResult(items=[_pod_checkout(restarts=0)], continue_token=None)

    adapter = FakeKubernetesAdapter(
        discovery=DiscoveryInfo(
            api_groups={"", "apps"},
            group_resources={"": {"pods", "services", "events"}, "apps": {"deployments"}},
        ),
        pages={
            ("", "pods", "demo", None): pod_pages,
            ("apps", "deployments", "demo", None): AdapterForbiddenError("403"),
            ("", "events", "demo", None): PageResult(
                items=[{"reason": "Scheduled", "involvedObject": {"kind": "Pod", "name": "p1"}, "message": "ok"}]
            ),
        },
        logs={
            ("demo", "checkout-abc", "web", False): AdapterNotFoundError("ContainerCreating"),
        },
    )

    collector = KubernetesEvidenceCollector()
    collector.register_target(
        TargetClusterConfig(context_alias="lab", allowed_namespaces=frozenset({"demo"})),
        k8s_adapter=adapter,
    )
    snap = collector.collect(
        TargetSelector(context_alias="lab", namespaces=["demo"]),
        CollectionLimits(max_pages=2, page_size=1),
    )

    assert snap.coverage["resources"].status == "truncated"
    assert snap.coverage["resources"].truncated is True
    assert "rbac_forbidden" in (snap.coverage["resources"].reason or "")
    assert snap.coverage["events"].status == "observed"
    assert snap.coverage["logs"].status == "observed_empty"
    assert snap.coverage["metrics"].status == "unsupported"
    assert snap.coverage["metrics"].count == 0


def test_client_adapter_handles_invalid_payload_redirects_and_status_codes():
    class FakeRestResponse:
        def __init__(self, status: int, body: bytes) -> None:
            self.status = status
            self.reason = "reason"
            self._body = body

        def read(self) -> bytes:
            return self._body

    cfg = k8s_client.Configuration()
    cfg.host = "https://k8s.example.internal"
    cfg.api_key = {"authorization": "tok"}
    cfg.api_key_prefix = {"authorization": "Bearer"}
    api_client = k8s_client.ApiClient(configuration=cfg)

    api_client.call_api = Mock(return_value=FakeRestResponse(302, b""))  # type: ignore[method-assign]
    k8s_adapter = KubernetesClientAdapter(api_client)
    with pytest.raises(AdapterRedirectError):
        k8s_adapter.discover(timeout_seconds=1.0)

    api_client.call_api = Mock(return_value=FakeRestResponse(200, b"{not-json"))  # type: ignore[method-assign]
    with pytest.raises(AdapterInvalidPayloadError):
        k8s_adapter.discover(timeout_seconds=1.0)

    api_client.call_api = Mock(return_value=FakeRestResponse(200, b'{"items": "bad"}'))  # type: ignore[method-assign]
    with pytest.raises(AdapterInvalidPayloadError):
        k8s_adapter.list_resource("", "v1", "pods", namespace="demo", label_selector=None, limit=10, continue_token=None, timeout_seconds=1.0)


def test_prometheus_adapter_enforces_fixed_queries_and_blocks_redirects():
    seen_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"Location": "https://other.invalid"})
        seen_queries.append(request.url.params["query"])
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "result": [
                        {
                            "metric": {"namespace": "demo", "pod": "checkout-abc", "container": "web"},
                            "value": [1763640000, "67108864"],
                        }
                    ]
                },
            },
        )

    prom = HttpPrometheusAdapter(
        "https://prom.example.internal",
        bearer_token="prom-canary-token",
        transport=httpx.MockTransport(handler),
    )
    items = prom.query_fixed("memory_working_set", namespaces=("demo",), timeout_seconds=2.0)
    assert len(items) == 1
    assert items[0]["source"] == "prometheus"
    assert 'namespace=~"demo"' in seen_queries[0]

    with pytest.raises(ForbiddenOperationError):
        prom.query_fixed("arbitrary_promql", namespaces=("demo",), timeout_seconds=2.0)

    redirect_prom = HttpPrometheusAdapter(
        "https://prom.example.internal",
        transport=httpx.MockTransport(lambda _req: httpx.Response(302, headers={"Location": "https://evil"})),
    )
    with pytest.raises(AdapterRedirectError):
        redirect_prom.query_fixed("memory_working_set", namespaces=("demo",), timeout_seconds=2.0)


def test_unreachable_target_uses_scope_matched_cache_and_isolates_other_scopes():
    now = {"t": 100.0}
    cache = SnapshotCache(clock=lambda: now["t"])
    collector = KubernetesEvidenceCollector(cache=cache)

    healthy_adapter = FakeKubernetesAdapter(
        pages={
            ("apps", "deployments", "demo", None): PageResult(items=[_deployment_checkout()]),
        }
    )
    failing_adapter = FakeKubernetesAdapter(discovery=AdapterUnreachableError("connection refused"))

    collector.register_target(
        TargetClusterConfig(context_alias="lab", allowed_namespaces=frozenset({"demo", "other"})),
        k8s_adapter=healthy_adapter,
    )
    live_snap = collector.collect(TargetSelector(context_alias="lab", namespaces=["demo"]))
    assert live_snap.source_type == "live"
    assert live_snap.has_usable_evidence()

    # Replace target adapter with failing transport
    collector.register_target(
        TargetClusterConfig(context_alias="lab", allowed_namespaces=frozenset({"demo", "other"})),
        k8s_adapter=failing_adapter,
    )
    cached_snap = collector.collect(TargetSelector(context_alias="lab", namespaces=["demo"]))
    assert cached_snap.source_type == "cached"
    assert cached_snap.is_degraded() is True
    assert cached_snap.observed_at == live_snap.observed_at

    # Different namespace must NOT receive cached snapshot from demo
    other_snap = collector.collect(TargetSelector(context_alias="lab", namespaces=["other"]))
    assert other_snap.source_type == "live"
    assert not other_snap.has_usable_evidence()
    assert other_snap.coverage["resources"].status == "unreachable"

    # Unallowlisted namespace or alias raises ScopeDeniedError
    with pytest.raises(ScopeDeniedError):
        collector.collect(TargetSelector(context_alias="lab", namespaces=["kube-system"]))
    with pytest.raises(ScopeDeniedError):
        collector.collect(TargetSelector(context_alias="prod", namespaces=["demo"]))


def test_deadline_and_context_isolation_across_two_clusters():
    adapter_a = FakeKubernetesAdapter(
        pages={("apps", "deployments", "demo", None): PageResult(items=[_deployment_checkout()])}
    )
    adapter_b = FakeKubernetesAdapter(discovery=AdapterTimeoutError("timeout"))

    collector = KubernetesEvidenceCollector()
    collector.register_target(
        TargetClusterConfig(context_alias="cluster-a", allowed_namespaces=frozenset({"demo"})),
        k8s_adapter=adapter_a,
    )
    collector.register_target(
        TargetClusterConfig(context_alias="cluster-b", allowed_namespaces=frozenset({"demo"})),
        k8s_adapter=adapter_b,
    )

    snap_a = collector.collect(TargetSelector(context_alias="cluster-a", namespaces=["demo"]))
    snap_b = collector.collect(TargetSelector(context_alias="cluster-b", namespaces=["demo"]))
    assert snap_a.has_usable_evidence()
    assert snap_b.coverage["resources"].status == "timeout"

    # Slow discovery hitting deadline returns timeout gracefully
    slow_adapter = FakeKubernetesAdapter(delay_seconds=0.05)
    collector.register_target(
        TargetClusterConfig(context_alias="slow", allowed_namespaces=frozenset({"demo"})),
        k8s_adapter=slow_adapter,
    )
    slow_snap = collector.collect(
        TargetSelector(context_alias="slow", namespaces=["demo"]),
        CollectionLimits(deadline_seconds=0.01),
    )
    assert slow_snap.coverage["resources"].status == "timeout"

    # Verify concurrency semaphore limits concurrent access per target
    gate = threading.Event()
    entered = threading.Event()

    class BlockingAdapter(FakeKubernetesAdapter):
        def discover(self, *, timeout_seconds: float) -> DiscoveryInfo:
            entered.set()
            gate.wait(timeout=1.0)
            return super().discover(timeout_seconds=timeout_seconds)

    conc_collector = KubernetesEvidenceCollector()
    conc_collector.register_target(
        TargetClusterConfig(context_alias="conc", allowed_namespaces=frozenset({"demo"})),
        k8s_adapter=BlockingAdapter(),
        max_concurrency=1,
    )
    t = threading.Thread(
        target=lambda: conc_collector.collect(TargetSelector(context_alias="conc", namespaces=["demo"]))
    )
    t.start()
    assert entered.wait(timeout=1.0)
    contended = conc_collector.collect(
        TargetSelector(context_alias="conc", namespaces=["demo"]),
        CollectionLimits(deadline_seconds=0.02),
    )
    gate.set()
    t.join(timeout=2.0)
    assert contended.coverage["resources"].status == "timeout"
    assert contended.coverage["resources"].reason == "collector_concurrency_timeout"


def test_collector_plugs_into_rca_api_service(tmp_path: Path):
    adapter = FakeKubernetesAdapter(
        discovery=DiscoveryInfo(
            api_groups={"", "apps", "discovery.k8s.io", "events.k8s.io"},
            group_resources={
                "": {"pods", "services", "events"},
                "apps": {"deployments"},
                "discovery.k8s.io": {"endpointslices"},
                "events.k8s.io": {"events"},
            },
        ),
        pages={("apps", "deployments", "demo", None): PageResult(items=[_deployment_checkout()])},
    )
    collector = KubernetesEvidenceCollector()
    collector.register_target(
        TargetClusterConfig(context_alias="lab", allowed_namespaces=frozenset({"demo"})),
        k8s_adapter=adapter,
    )

    config = ApiConfig(
        ui_tokens=frozenset({"ui-tok"}),
        operator_tokens=frozenset({"op-tok"}),
        allowed_scopes={"lab": frozenset({"demo"})},
        db_path=tmp_path / "api.db",
    )
    store = RunStore(config.db_path)
    store.initialize()
    service = RcaApiService(
        config=config,
        store=store,
        complete=Mock(return_value="Checkout 64Mi memory limit is too small."),
        collector=build_collector_fn(collector),
    )
    try:
        run = asyncio.run(
            service.execute_run(
                request_id="req-api-collect-1",
                variant="base",
                target=TargetSelector(context_alias="lab", namespaces=["demo"]),
                incident="Checkout unavailable",
            )
        )
        assert run.status == "qualified"  # metrics unsupported -> qualified
        assert any("metrics: unsupported" in item for item in run.limitations)
        assert run.result == "Checkout 64Mi memory limit is too small."
    finally:
        service.shutdown()
        store.close()
