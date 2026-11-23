"""Read-only Kubernetes evidence collector, isolated adapters, and safety guards."""

from __future__ import annotations

import atexit
import base64
import binascii
import json
import re
import ssl
import tempfile
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any, Protocol
from urllib.parse import quote, urlencode, urlparse

import httpx
import kubernetes.client as k8s_client
import urllib3
import yaml
from kubernetes.client.rest import ApiException

from rca.contracts.runtime import (
    CANONICAL_EVIDENCE_SOURCES,
    CollectorFn,
    CoverageStatus,
    EvidenceSnapshot,
    SourceCoverage,
    TargetSelector,
    utc_now_iso,
)
from rca.evidence.redaction import redact_reason, redact_text, redact_value, sanitize_resource
from rca.evidence.snapshot import CollectionLimits, SnapshotCache, build_snapshot

_VALID_NAMESPACE_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_COREDNS_NAMES = frozenset({"coredns", "kube-dns"})
_PROMETHEUS_FIXED_QUERIES: dict[str, str] = {
    "memory_working_set": (
        'sum by (namespace, pod, container) '
        '(container_memory_working_set_bytes{{namespace=~"{ns}", container!="", container!="POD"}})'
    ),
    "cpu_usage_rate": (
        'sum by (namespace, pod, container) '
        '(rate(container_cpu_usage_seconds_total{{namespace=~"{ns}", container!="", container!="POD"}}[5m]))'
    ),
    "cpu_throttled_ratio": (
        'sum by (namespace, pod, container) '
        '(rate(container_cpu_cfs_throttled_periods_total{{namespace=~"{ns}", container!="", container!="POD"}}[5m]))'
    ),
    "container_restarts": (
        'sum by (namespace, pod, container) '
        '(kube_pod_container_status_restarts_total{{namespace=~"{ns}"}})'
    ),
}

READ_ONLY_ALLOWED_RESOURCES: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("", "v1", "pods"),
        ("", "v1", "services"),
        ("", "v1", "events"),
        ("", "v1", "configmaps"),
        ("", "v1", "persistentvolumeclaims"),
        ("", "v1", "persistentvolumes"),
        ("", "v1", "serviceaccounts"),
        ("", "v1", "endpoints"),
        ("", "v1", "nodes"),
        ("apps", "v1", "deployments"),
        ("apps", "v1", "replicasets"),
        ("apps", "v1", "daemonsets"),
        ("apps", "v1", "statefulsets"),
        ("discovery.k8s.io", "v1", "endpointslices"),
        ("networking.k8s.io", "v1", "networkpolicies"),
        ("networking.k8s.io", "v1", "ingresses"),
        ("gateway.networking.k8s.io", "v1", "gateways"),
        ("gateway.networking.k8s.io", "v1", "httproutes"),
        ("storage.k8s.io", "v1", "storageclasses"),
        ("storage.k8s.io", "v1", "csidrivers"),
        ("storage.k8s.io", "v1", "csinodes"),
        ("rbac.authorization.k8s.io", "v1", "roles"),
        ("rbac.authorization.k8s.io", "v1", "rolebindings"),
        ("rbac.authorization.k8s.io", "v1", "clusterroles"),
        ("rbac.authorization.k8s.io", "v1", "clusterrolebindings"),
        ("events.k8s.io", "v1", "events"),
        ("metrics.k8s.io", "v1beta1", "pods"),
        ("metrics.k8s.io", "v1beta1", "nodes"),
    }
)


class EvidenceCollectionError(RuntimeError):
    """Base error for evidence collector setup or scope failures."""


class KubeconfigSecurityError(ValueError):
    """Raised when a kubeconfig violates isolation or TLS/exec security rules."""


class ScopeDeniedError(PermissionError):
    """Raised when a requested alias or namespace is outside the configured allowlist."""


class ForbiddenOperationError(PermissionError):
    """Raised when code attempts a disallowed Kubernetes resource or PromQL query."""


class AdapterError(RuntimeError):
    """Base typed error raised by Kubernetes or telemetry adapters."""


class AdapterAuthError(AdapterError):
    """Raised on HTTP 401 authentication failure."""


class AdapterForbiddenError(AdapterError):
    """Raised on HTTP 403 RBAC authorization failure."""


class AdapterNotFoundError(AdapterError):
    """Raised on HTTP 404 when a resource kind or endpoint is absent/unsupported."""


class AdapterResourceExpiredError(AdapterError):
    """Raised on HTTP 410 Gone when a list continue token expires."""


class AdapterRedirectError(AdapterError):
    """Raised when an upstream server attempts an HTTP 3xx redirect."""


class AdapterTimeoutError(AdapterError, TimeoutError):
    """Raised when an upstream request times out."""


class AdapterTlsError(AdapterError):
    """Raised on TLS or CA verification failure."""


class AdapterUnreachableError(AdapterError):
    """Raised when an upstream API or telemetry endpoint cannot be reached."""


class AdapterInvalidPayloadError(AdapterError, ValueError):
    """Raised when an upstream API returns malformed or unexpected JSON shapes."""


@dataclass(frozen=True)
class PageResult:
    """Single paginated Kubernetes list response."""

    items: list[dict[str, Any]]
    continue_token: str | None = None


@dataclass(frozen=True)
class DiscoveryInfo:
    """Discovered Kubernetes API groups and group resources."""

    api_groups: set[str]
    group_resources: dict[str, set[str]] = field(default_factory=dict)

    def has_resource(self, group: str, plural: str) -> bool:
        if group and group not in self.api_groups and group not in self.group_resources:
            return False
        resources = self.group_resources.get(group)
        if resources is None:
            return True
        return plural in resources


class KubernetesAdapter(Protocol):
    """Typed read-only adapter boundary for Kubernetes API discovery, list, and logs."""

    def discover(self, *, timeout_seconds: float) -> DiscoveryInfo: ...

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
    ) -> PageResult: ...

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
    ) -> str: ...


class PrometheusAdapter(Protocol):
    """Typed bounded read-only adapter for optional retained Prometheus queries."""

    def query_fixed(
        self,
        query_name: str,
        *,
        namespaces: tuple[str, ...],
        timeout_seconds: float,
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class TargetClusterConfig:
    """Immutable per-target cluster configuration and namespace allowlist."""

    context_alias: str
    allowed_namespaces: frozenset[str]
    kubeconfig_path: Path | None = None
    context_name: str | None = None
    prometheus_url: str | None = None
    prometheus_ca_file: Path | None = None
    prometheus_bearer_token: str | None = field(default=None, repr=False)
    extra_secrets: tuple[str, ...] = field(default=(), repr=False)


def assert_resource_allowed(group: str, version: str, plural: str) -> None:
    """Reject Secrets and any resource outside the read-only allowlist."""
    if plural == "secrets":
        raise ForbiddenOperationError("Reading Kubernetes Secret resources is prohibited.")
    if (group, version, plural) not in READ_ONLY_ALLOWED_RESOURCES:
        raise ForbiddenOperationError(
            f"Kubernetes resource {group or 'core'}/{version}/{plural} is not allowlisted."
        )


def _decode_base64_material(raw: Any, label: str) -> bytes:
    if not isinstance(raw, str) or not raw.strip():
        raise KubeconfigSecurityError(f"Kubeconfig {label} must be a non-empty base64 string.")
    try:
        return base64.b64decode(raw.strip(), validate=True)
    except (binascii.Error, ValueError) as error:
        raise KubeconfigSecurityError(f"Kubeconfig {label} contains invalid base64.") from error


def _resolve_kubeconfig_file(path_value: Any, base_dir: Path, label: str) -> Path:
    if not isinstance(path_value, str) or not path_value.strip():
        raise KubeconfigSecurityError(f"Kubeconfig {label} must be a non-empty file path.")
    candidate = Path(path_value.strip())
    if not candidate.is_absolute():
        candidate = (base_dir / candidate).resolve()
    if not candidate.is_file():
        raise KubeconfigSecurityError(f"Kubeconfig {label} file does not exist.")
    return candidate


def _write_temp_pem(temp_dir: Path, filename: str, data: bytes) -> str:
    target = temp_dir / filename
    target.write_bytes(data)
    target.chmod(0o600)
    return str(target)


def load_isolated_kube_configuration(
    kubeconfig_path: Path,
    context_name: str,
) -> tuple[k8s_client.Configuration, tuple[str, ...]]:
    """Load an isolated kubernetes Configuration for one named context.

    Does not touch process-global ``Configuration._default`` or ambient
    ``~/.kube/config``, rejects ``exec`` and ``auth-provider`` plugins, and
    enforces HTTPS with TLS verification enabled.
    """
    if not kubeconfig_path.is_file():
        raise KubeconfigSecurityError(f"Kubeconfig file does not exist: {kubeconfig_path}")

    try:
        parsed = yaml.safe_load(kubeconfig_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise KubeconfigSecurityError("Failed to read kubeconfig YAML.") from error

    if not isinstance(parsed, Mapping):
        raise KubeconfigSecurityError("Kubeconfig root must be a YAML mapping.")

    contexts = parsed.get("contexts")
    clusters = parsed.get("clusters")
    users = parsed.get("users")
    if not isinstance(contexts, list) or not isinstance(clusters, list) or not isinstance(users, list):
        raise KubeconfigSecurityError("Kubeconfig must define contexts, clusters, and users lists.")

    context_entry = next(
        (
            item.get("context")
            for item in contexts
            if isinstance(item, Mapping) and item.get("name") == context_name
        ),
        None,
    )
    if not isinstance(context_entry, Mapping):
        raise KubeconfigSecurityError(f"Named context {context_name!r} not found in kubeconfig.")

    cluster_name = context_entry.get("cluster")
    user_name = context_entry.get("user")
    cluster_entry = next(
        (
            item.get("cluster")
            for item in clusters
            if isinstance(item, Mapping) and item.get("name") == cluster_name
        ),
        None,
    )
    user_entry = next(
        (
            item.get("user")
            for item in users
            if isinstance(item, Mapping) and item.get("name") == user_name
        ),
        None,
    )
    if not isinstance(cluster_entry, Mapping):
        raise KubeconfigSecurityError(f"Cluster {cluster_name!r} not found in kubeconfig.")
    if not isinstance(user_entry, Mapping):
        raise KubeconfigSecurityError(f"User {user_name!r} not found in kubeconfig.")

    if user_entry.get("exec") is not None:
        raise KubeconfigSecurityError("Kubeconfig user exec plugins are prohibited.")
    if user_entry.get("auth-provider") is not None:
        raise KubeconfigSecurityError("Kubeconfig user auth-provider plugins are prohibited.")
    if cluster_entry.get("insecure-skip-tls-verify"):
        raise KubeconfigSecurityError("insecure-skip-tls-verify=true is prohibited.")

    server = cluster_entry.get("server")
    if not isinstance(server, str) or not server.strip():
        raise KubeconfigSecurityError("Cluster server URL is required.")
    parsed_url = urlparse(server.strip())
    if parsed_url.scheme.lower() != "https" or not parsed_url.hostname:
        raise KubeconfigSecurityError("Cluster server URL must use https:// with a valid host.")
    if parsed_url.username is not None or parsed_url.password is not None:
        raise KubeconfigSecurityError("Embedded credentials in cluster server URL are prohibited.")

    base_dir = kubeconfig_path.resolve().parent
    config = k8s_client.Configuration()
    config.host = server.strip().rstrip("/")
    config.verify_ssl = True
    config.retries = 0

    secrets: list[str] = []
    temp_dir: Path | None = None

    def _ensure_temp_dir() -> Path:
        nonlocal temp_dir
        if temp_dir is None:
            temp_dir = Path(tempfile.mkdtemp(prefix="rcannon-kube-"))
            atexit.register(lambda d=temp_dir: _cleanup_dir(d))
        return temp_dir

    ca_data = cluster_entry.get("certificate-authority-data")
    ca_file = cluster_entry.get("certificate-authority")
    if ca_data is not None:
        raw_ca = _decode_base64_material(ca_data, "certificate-authority-data")
        secrets.append(str(ca_data))
        config.ssl_ca_cert = _write_temp_pem(_ensure_temp_dir(), "ca.crt", raw_ca)
    elif ca_file is not None:
        config.ssl_ca_cert = str(
            _resolve_kubeconfig_file(ca_file, base_dir, "certificate-authority")
        )

    client_cert_data = user_entry.get("client-certificate-data")
    client_cert_file = user_entry.get("client-certificate")
    if client_cert_data is not None:
        raw_cert = _decode_base64_material(client_cert_data, "client-certificate-data")
        secrets.append(str(client_cert_data))
        config.cert_file = _write_temp_pem(_ensure_temp_dir(), "client.crt", raw_cert)
    elif client_cert_file is not None:
        config.cert_file = str(
            _resolve_kubeconfig_file(client_cert_file, base_dir, "client-certificate")
        )

    client_key_data = user_entry.get("client-key-data")
    client_key_file = user_entry.get("client-key")
    if client_key_data is not None:
        raw_key = _decode_base64_material(client_key_data, "client-key-data")
        secrets.append(str(client_key_data))
        secrets.append(raw_key.decode("utf-8", errors="ignore"))
        config.key_file = _write_temp_pem(_ensure_temp_dir(), "client.key", raw_key)
    elif client_key_file is not None:
        config.key_file = str(_resolve_kubeconfig_file(client_key_file, base_dir, "client-key"))

    token = user_entry.get("token")
    token_file = user_entry.get("tokenFile")
    if isinstance(token, str) and token.strip():
        tok = token.strip()
        secrets.append(tok)
        config.api_key = {"authorization": tok}
        config.api_key_prefix = {"authorization": "Bearer"}
    elif token_file is not None:
        tok_path = _resolve_kubeconfig_file(token_file, base_dir, "tokenFile")
        tok = tok_path.read_text(encoding="utf-8").strip()
        if not tok:
            raise KubeconfigSecurityError("Kubeconfig tokenFile is empty.")
        secrets.append(tok)
        config.api_key = {"authorization": tok}
        config.api_key_prefix = {"authorization": "Bearer"}

    return config, tuple(s for s in secrets if s)


def _cleanup_dir(path: Path) -> None:
    if not path.exists():
        return
    for child in path.iterdir():
        try:
            child.unlink()
        except OSError:
            pass
    try:
        path.rmdir()
    except OSError:
        pass


def build_isolated_api_client(config: k8s_client.Configuration) -> k8s_client.ApiClient:
    """Create a dedicated ApiClient with redirects disabled."""
    client = k8s_client.ApiClient(configuration=config)
    pool_manager = getattr(client.rest_client, "pool_manager", None)
    if pool_manager is not None and hasattr(pool_manager, "connection_pool_kw"):
        pool_manager.connection_pool_kw["retries"] = urllib3.Retry(
            total=0,
            redirect=False,
            raise_on_redirect=False,
        )
    return client


def _map_k8s_exception(error: Exception) -> AdapterError:
    if isinstance(error, AdapterError):
        return error
    if isinstance(error, ApiException):
        status = getattr(error, "status", 0) or 0
        reason = str(getattr(error, "reason", "") or "")
        if 300 <= status < 400:
            return AdapterRedirectError("Kubernetes API HTTP redirects are disallowed.")
        if status == 401:
            return AdapterAuthError("Kubernetes API authentication failed (HTTP 401).")
        if status == 403:
            return AdapterForbiddenError("Kubernetes API access denied (HTTP 403).")
        if status == 404:
            return AdapterNotFoundError("Kubernetes API resource not found (HTTP 404).")
        if status == 410:
            return AdapterResourceExpiredError("Kubernetes list resourceVersion expired (HTTP 410).")
        lowered = reason.lower()
        if "ssl" in lowered or "certificate" in lowered:
            return AdapterTlsError("Kubernetes API TLS verification failed.")
        if "timeout" in lowered or "timed out" in lowered:
            return AdapterTimeoutError("Kubernetes API request timed out.")
        return AdapterUnreachableError(f"Kubernetes API request failed (status {status}).")

    lowered = str(error).lower()
    if isinstance(error, ssl.SSLError) or "ssl" in lowered or "certificate" in lowered:
        return AdapterTlsError("Kubernetes API TLS verification failed.")
    if isinstance(error, TimeoutError) or "timed out" in lowered or "timeout" in lowered:
        return AdapterTimeoutError("Kubernetes API request timed out.")
    return AdapterUnreachableError("Kubernetes API endpoint is unreachable.")


class KubernetesClientAdapter:
    """Read-only KubernetesAdapter backed by an isolated ApiClient."""

    def __init__(self, api_client: k8s_client.ApiClient) -> None:
        self._client = api_client

    def _request_bytes(
        self,
        path: str,
        *,
        accept: str,
        timeout_seconds: float,
    ) -> bytes:
        headers: dict[str, str] = {"Accept": accept}
        queries: list[tuple[str, str]] = []
        self._client.update_params_for_auth(
            headers,
            queries,
            ["BearerToken"],
            path,
            "GET",
            None,
        )
        host = str(self._client.configuration.host or "").rstrip("/")
        url = f"{host}{path}"
        try:
            response = self._client.call_api(
                "GET",
                url,
                header_params=headers,
                _request_timeout=timeout_seconds,
            )
            status = getattr(response, "status", None)
            if isinstance(status, int):
                if 300 <= status < 400:
                    raise AdapterRedirectError("Kubernetes API HTTP redirects are disallowed.")
                if status == 401:
                    raise AdapterAuthError("Kubernetes API authentication failed (HTTP 401).")
                if status == 403:
                    raise AdapterForbiddenError("Kubernetes API access denied (HTTP 403).")
                if status == 404:
                    raise AdapterNotFoundError("Kubernetes API resource not found (HTTP 404).")
                if status == 410:
                    raise AdapterResourceExpiredError(
                        "Kubernetes list resourceVersion expired (HTTP 410)."
                    )
                if status >= 400:
                    raise AdapterUnreachableError(
                        f"Kubernetes API request failed (status {status})."
                    )
            raw_bytes = response.read() if hasattr(response, "read") else getattr(response, "data", b"")
        except Exception as error:  # noqa: BLE001
            raise _map_k8s_exception(error) from error

        if isinstance(raw_bytes, bytes):
            return raw_bytes
        if raw_bytes is None:
            return b""
        return str(raw_bytes).encode("utf-8")

    def _get_json(self, path: str, *, timeout_seconds: float) -> dict[str, Any]:
        raw_bytes = self._request_bytes(
            path,
            accept="application/json",
            timeout_seconds=timeout_seconds,
        )
        try:
            parsed = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AdapterInvalidPayloadError("Kubernetes API returned invalid JSON.") from error
        if not isinstance(parsed, dict):
            raise AdapterInvalidPayloadError("Kubernetes API response must be a JSON object.")
        return parsed

    def discover(self, *, timeout_seconds: float) -> DiscoveryInfo:
        groups: set[str] = {""}
        group_resources: dict[str, set[str]] = {}

        core = self._get_json("/api/v1", timeout_seconds=timeout_seconds)
        raw_core_res = core.get("resources")
        if isinstance(raw_core_res, list):
            group_resources[""] = {
                str(r["name"])
                for r in raw_core_res
                if isinstance(r, Mapping) and isinstance(r.get("name"), str)
            }

        apis = self._get_json("/apis", timeout_seconds=timeout_seconds)
        raw_groups = apis.get("groups")
        if isinstance(raw_groups, list):
            for entry in raw_groups:
                if isinstance(entry, Mapping) and isinstance(entry.get("name"), str):
                    groups.add(entry["name"])

        return DiscoveryInfo(api_groups=groups, group_resources=group_resources)

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
        prefix = f"/api/{version}" if not group else f"/apis/{group}/{version}"
        if namespace is not None:
            base_path = f"{prefix}/namespaces/{quote(namespace, safe='')}/{plural}"
        else:
            base_path = f"{prefix}/{plural}"

        query: dict[str, str | int] = {"limit": limit}
        if label_selector:
            query["labelSelector"] = label_selector
        if continue_token:
            query["continue"] = continue_token
        payload = self._get_json(f"{base_path}?{urlencode(query)}", timeout_seconds=timeout_seconds)

        raw_items = payload.get("items")
        if not isinstance(raw_items, list) or not all(isinstance(x, dict) for x in raw_items):
            raise AdapterInvalidPayloadError(
                f"Kubernetes list response for {plural} must contain a list of item objects."
            )
        metadata = payload.get("metadata")
        next_token: str | None = None
        if isinstance(metadata, Mapping):
            raw_next = metadata.get("continue")
            if isinstance(raw_next, str) and raw_next:
                next_token = raw_next
        return PageResult(items=raw_items, continue_token=next_token)

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
        query = urlencode(
            {
                "container": container,
                "previous": "true" if previous else "false",
                "tailLines": tail_lines,
                "limitBytes": limit_bytes,
                "timestamps": "true",
            }
        )
        path = (
            f"/api/v1/namespaces/{quote(namespace, safe='')}/pods/"
            f"{quote(pod_name, safe='')}/log?{query}"
        )
        raw_bytes = self._request_bytes(
            path,
            accept="text/plain",
            timeout_seconds=timeout_seconds,
        )
        return raw_bytes.decode("utf-8", errors="replace")


class HttpPrometheusAdapter:
    """Bounded read-only Prometheus HTTP query adapter with fixed allowlisted queries."""

    def __init__(
        self,
        base_url: str,
        *,
        ca_file: Path | None = None,
        bearer_token: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        parsed = urlparse(base_url.strip())
        if parsed.scheme.lower() not in {"https", "http"} or not parsed.netloc:
            raise ValueError("Prometheus URL must be a valid http(s) URL.")
        self._base_url = base_url.strip().rstrip("/")
        self._verify: bool | str = str(ca_file) if ca_file is not None else True
        self._bearer_token = bearer_token
        self._transport = transport

    def query_fixed(
        self,
        query_name: str,
        *,
        namespaces: tuple[str, ...],
        timeout_seconds: float,
    ) -> list[dict[str, Any]]:
        template = _PROMETHEUS_FIXED_QUERIES.get(query_name)
        if template is None:
            raise ForbiddenOperationError(
                f"Prometheus query {query_name!r} is outside the fixed read-only allowlist."
            )
        for ns in namespaces:
            if not _VALID_NAMESPACE_RE.fullmatch(ns):
                raise ScopeDeniedError(f"Invalid namespace for Prometheus query: {ns!r}.")
        ns_regex = "|".join(namespaces)
        promql = template.format(ns=ns_regex)

        headers: dict[str, str] = {"Accept": "application/json"}
        if self._bearer_token:
            headers["Authorization"] = f"Bearer {self._bearer_token}"

        try:
            with httpx.Client(
                follow_redirects=False,
                verify=self._verify,
                timeout=timeout_seconds,
                transport=self._transport,
            ) as client:
                response = client.get(
                    f"{self._base_url}/api/v1/query",
                    params={"query": promql},
                    headers=headers,
                )
        except httpx.TimeoutException as error:
            raise AdapterTimeoutError("Prometheus query timed out.") from error
        except httpx.TransportError as error:
            lowered = str(error).lower()
            if "ssl" in lowered or "certificate" in lowered:
                raise AdapterTlsError("Prometheus TLS verification failed.") from error
            raise AdapterUnreachableError("Prometheus endpoint is unreachable.") from error

        if 300 <= response.status_code < 400:
            raise AdapterRedirectError("Prometheus HTTP redirects are disallowed.")
        if response.status_code == 401:
            raise AdapterAuthError("Prometheus authentication failed (HTTP 401).")
        if response.status_code == 403:
            raise AdapterForbiddenError("Prometheus access denied (HTTP 403).")
        if response.status_code == 404:
            raise AdapterNotFoundError("Prometheus query endpoint not found (HTTP 404).")
        if response.status_code >= 400:
            raise AdapterUnreachableError(
                f"Prometheus query failed with HTTP {response.status_code}."
            )

        try:
            body = response.json()
        except ValueError as error:
            raise AdapterInvalidPayloadError("Prometheus returned invalid JSON.") from error
        if not isinstance(body, Mapping) or body.get("status") != "success":
            raise AdapterInvalidPayloadError("Prometheus response status was not 'success'.")
        data = body.get("data")
        results = data.get("result") if isinstance(data, Mapping) else None
        if not isinstance(results, list):
            raise AdapterInvalidPayloadError("Prometheus response data.result must be a list.")

        records: list[dict[str, Any]] = []
        for entry in results:
            if not isinstance(entry, Mapping):
                continue
            metric = entry.get("metric")
            value_pair = entry.get("value")
            if not isinstance(metric, Mapping):
                continue
            records.append(
                {
                    "source": "prometheus",
                    "query": query_name,
                    "metric": dict(metric),
                    "value": value_pair[1] if isinstance(value_pair, list) and len(value_pair) > 1 else None,
                    "timestamp": value_pair[0] if isinstance(value_pair, list) and value_pair else None,
                }
            )
        return records


@dataclass
class _RegisteredTarget:
    config: TargetClusterConfig
    k8s_adapter: KubernetesAdapter
    prometheus_adapter: PrometheusAdapter | None
    secrets: tuple[str, ...]
    semaphore: threading.Semaphore


def _error_to_coverage_status(error: AdapterError) -> tuple[CoverageStatus, str]:
    if isinstance(error, AdapterForbiddenError):
        return "forbidden", "rbac_forbidden"
    if isinstance(error, AdapterAuthError):
        return "forbidden", "auth_failed"
    if isinstance(error, AdapterNotFoundError):
        return "unsupported", "api_resource_not_found"
    if isinstance(error, AdapterTimeoutError):
        return "timeout", "request_timed_out"
    if isinstance(error, AdapterTlsError):
        return "unreachable", "tls_verification_failed"
    if isinstance(error, AdapterRedirectError):
        return "invalid", "redirect_disallowed"
    if isinstance(error, AdapterInvalidPayloadError):
        return "invalid", "invalid_api_payload"
    if isinstance(error, AdapterResourceExpiredError):
        return "truncated", "resource_version_expired"
    return "unreachable", "api_unreachable"


def _worst_status(statuses: Sequence[CoverageStatus], fallback: CoverageStatus) -> CoverageStatus:
    priority: dict[CoverageStatus, int] = {
        "timeout": 7,
        "unreachable": 6,
        "forbidden": 5,
        "invalid": 4,
        "truncated": 3,
        "unsupported": 2,
        "not_requested": 1,
        "observed_empty": 0,
        "observed": 0,
    }
    if not statuses:
        return fallback
    return max(statuses, key=lambda s: priority.get(s, 0))


def _pod_sort_key(pod: Mapping[str, Any]) -> tuple[int, int, str]:
    status = pod.get("status") if isinstance(pod.get("status"), Mapping) else {}
    phase = str(status.get("phase") or "")
    restarts = 0
    unready = 0
    for c_status in status.get("containerStatuses") or []:
        if not isinstance(c_status, Mapping):
            continue
        restarts += int(c_status.get("restartCount") or 0)
        if not c_status.get("ready", False):
            unready += 1
    meta = pod.get("metadata") if isinstance(pod.get("metadata"), Mapping) else {}
    name = str(meta.get("name") or "")
    return (0 if phase != "Running" or unready > 0 else 1, -restarts, name)


def _pod_containers(pod: Mapping[str, Any]) -> list[tuple[str, int]]:
    spec = pod.get("spec") if isinstance(pod.get("spec"), Mapping) else {}
    status = pod.get("status") if isinstance(pod.get("status"), Mapping) else {}
    restarts_by_name: dict[str, int] = {}
    for cs in status.get("containerStatuses") or []:
        if isinstance(cs, Mapping) and isinstance(cs.get("name"), str):
            restarts_by_name[cs["name"]] = int(cs.get("restartCount") or 0)

    containers: list[tuple[str, int]] = []
    for key in ("containers", "initContainers"):
        for c in spec.get(key) or []:
            if isinstance(c, Mapping) and isinstance(c.get("name"), str):
                cname = c["name"]
                containers.append((cname, restarts_by_name.get(cname, 0)))
    return containers


def _normalize_event(raw: Mapping[str, Any]) -> dict[str, Any]:
    involved = raw.get("involvedObject") or raw.get("regarding") or {}
    if isinstance(involved, Mapping):
        kind = involved.get("kind") or ""
        name = involved.get("name") or ""
        involved_str = f"{kind}/{name}".strip("/")
    else:
        involved_str = str(involved)
    meta = raw.get("metadata") if isinstance(raw.get("metadata"), Mapping) else {}
    return {
        "namespace": meta.get("namespace"),
        "reason": raw.get("reason") or "",
        "type": raw.get("type") or "",
        "involved_object": involved_str,
        "message": raw.get("message") or raw.get("note") or "",
        "count": raw.get("count") or (raw.get("series") or {}).get("count") or 1,
        "first_timestamp": raw.get("firstTimestamp") or raw.get("eventTime") or meta.get("creationTimestamp"),
        "last_timestamp": raw.get("lastTimestamp") or raw.get("eventTime") or meta.get("creationTimestamp"),
    }


def _deduplicate_events(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str, str, str]] = set()
    unique: list[dict[str, Any]] = []
    for ev in events:
        sig = (
            str(ev.get("namespace") or ""),
            str(ev.get("reason") or ""),
            str(ev.get("involved_object") or ""),
            str(ev.get("message") or ""),
            str(ev.get("last_timestamp") or ""),
        )
        if sig in seen:
            continue
        seen.add(sig)
        unique.append(ev)
    return unique


def _derive_readonly_checks(
    resources: Sequence[Mapping[str, Any]],
    *,
    coredns_checked: bool,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    endpointslices_by_svc: dict[tuple[str, str], int] = {}
    storageclasses: set[str] = set()
    coredns_endpoints = 0

    for res in resources:
        kind = res.get("kind")
        meta = res.get("metadata") if isinstance(res.get("metadata"), Mapping) else {}
        ns = str(meta.get("namespace") or "")
        name = str(meta.get("name") or "")
        if kind == "EndpointSlice":
            labels = meta.get("labels") if isinstance(meta.get("labels"), Mapping) else {}
            svc_name = str(labels.get("kubernetes.io/service-name") or "")
            ready_addrs = sum(
                len(ep.get("addresses") or [])
                for ep in (res.get("endpoints") or [])
                if isinstance(ep, Mapping)
                and (not isinstance(ep.get("conditions"), Mapping) or ep["conditions"].get("ready", True))
            )
            if svc_name:
                endpointslices_by_svc[(ns, svc_name)] = (
                    endpointslices_by_svc.get((ns, svc_name), 0) + ready_addrs
                )
            if ns == "kube-system" and name.startswith(("coredns", "kube-dns")):
                coredns_endpoints += ready_addrs
        elif kind == "Endpoints":
            ready_addrs = sum(
                len(sub.get("addresses") or [])
                for sub in (res.get("subsets") or [])
                if isinstance(sub, Mapping)
            )
            endpointslices_by_svc.setdefault((ns, name), ready_addrs)
            if ns == "kube-system" and name in _COREDNS_NAMES:
                coredns_endpoints += ready_addrs
        elif kind == "StorageClass" and name:
            storageclasses.add(name)

    for res in resources:
        kind = res.get("kind")
        meta = res.get("metadata") if isinstance(res.get("metadata"), Mapping) else {}
        ns = str(meta.get("namespace") or "")
        name = str(meta.get("name") or "")
        spec = res.get("spec") if isinstance(res.get("spec"), Mapping) else {}

        if kind == "Service" and name and ns != "kube-system":
            if (ns, name) in endpointslices_by_svc:
                ready = endpointslices_by_svc[(ns, name)]
                checks.append(
                    {
                        "kind": "service_endpoints",
                        "target": f"Service/{ns}/{name}",
                        "status": "passed" if ready > 0 else "failed",
                        "details": f"Observed {ready} ready endpoint address(es).",
                    }
                )
        elif kind == "PersistentVolumeClaim" and name:
            sc_name = spec.get("storageClassName")
            if isinstance(sc_name, str) and sc_name:
                exists = sc_name in storageclasses
                checks.append(
                    {
                        "kind": "pvc_storageclass",
                        "target": f"PersistentVolumeClaim/{ns}/{name}",
                        "status": "passed" if exists else "failed",
                        "details": (
                            f"StorageClass/{sc_name} present in inventory."
                            if exists
                            else f"StorageClass/{sc_name} absent from observed StorageClass inventory."
                        ),
                    }
                )

    if coredns_checked:
        checks.append(
            {
                "kind": "dns_config_state",
                "target": "kube-system/coredns",
                "status": "passed" if coredns_endpoints > 0 else "failed",
                "details": (
                    f"CoreDNS API config/endpoint inspection observed {coredns_endpoints} ready endpoint(s) "
                    "(API state check, not an in-cluster DNS probe)."
                ),
            }
        )
    return checks


class KubernetesEvidenceCollector:
    """Thread-safe management-host collector supporting multiple isolated cluster targets."""

    def __init__(self, *, cache: SnapshotCache | None = None) -> None:
        self._lock = threading.RLock()
        self._targets: dict[str, _RegisteredTarget] = {}
        self._cache = cache or SnapshotCache()

    @property
    def cache(self) -> SnapshotCache:
        return self._cache

    def register_target(
        self,
        config: TargetClusterConfig,
        *,
        k8s_adapter: KubernetesAdapter | None = None,
        prometheus_adapter: PrometheusAdapter | None = None,
        max_concurrency: int = 4,
    ) -> None:
        """Register an immutable target cluster alias with an isolated client adapter."""
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be greater than zero.")
        secrets = list(config.extra_secrets)
        if config.prometheus_bearer_token:
            secrets.append(config.prometheus_bearer_token)

        active_k8s = k8s_adapter
        if active_k8s is None:
            if config.kubeconfig_path is None or not config.context_name:
                raise EvidenceCollectionError(
                    "kubeconfig_path and context_name are required when k8s_adapter is omitted."
                )
            k8s_cfg, kube_secrets = load_isolated_kube_configuration(
                config.kubeconfig_path,
                config.context_name,
            )
            secrets.extend(kube_secrets)
            active_k8s = KubernetesClientAdapter(build_isolated_api_client(k8s_cfg))

        active_prom = prometheus_adapter
        if active_prom is None and config.prometheus_url:
            active_prom = HttpPrometheusAdapter(
                config.prometheus_url,
                ca_file=config.prometheus_ca_file,
                bearer_token=config.prometheus_bearer_token,
            )

        entry = _RegisteredTarget(
            config=config,
            k8s_adapter=active_k8s,
            prometheus_adapter=active_prom,
            secrets=tuple(s for s in secrets if s),
            semaphore=threading.Semaphore(max_concurrency),
        )
        with self._lock:
            self._targets[config.context_alias] = entry

    def _get_target(self, target: TargetSelector) -> _RegisteredTarget:
        with self._lock:
            entry = self._targets.get(target.context_alias)
        if entry is None:
            raise ScopeDeniedError(
                f"Target context_alias {target.context_alias!r} is not allowlisted."
            )
        disallowed = [ns for ns in target.namespaces if ns not in entry.config.allowed_namespaces]
        if disallowed:
            raise ScopeDeniedError(
                f"Namespace(s) outside allowlist for {target.context_alias!r}: {', '.join(disallowed)}."
            )
        return entry

    def _remaining_timeout(self, deadline_at: float, per_request: float) -> float:
        remaining = deadline_at - monotonic()
        if remaining <= 0:
            raise AdapterTimeoutError("Collection deadline exceeded.")
        return min(per_request, remaining)

    def _list_all_pages(
        self,
        adapter: KubernetesAdapter,
        *,
        group: str,
        version: str,
        plural: str,
        namespace: str | None,
        label_selector: str | None,
        limits: CollectionLimits,
        deadline_at: float,
        max_items: int,
    ) -> tuple[list[dict[str, Any]], bool, str | None]:
        assert_resource_allowed(group, version, plural)
        items: list[dict[str, Any]] = []
        continue_token: str | None = None
        truncated = False
        reason: str | None = None
        retried_410 = False

        page = 0
        while page < limits.max_pages and len(items) < max_items:
            timeout = self._remaining_timeout(deadline_at, limits.per_request_timeout_seconds)
            try:
                result = adapter.list_resource(
                    group,
                    version,
                    plural,
                    namespace=namespace,
                    label_selector=label_selector,
                    limit=min(limits.page_size, max_items - len(items)),
                    continue_token=continue_token,
                    timeout_seconds=timeout,
                )
            except AdapterResourceExpiredError:
                if not retried_410 and continue_token is not None:
                    retried_410 = True
                    items = []
                    continue_token = None
                    page = 0
                    continue
                truncated = True
                reason = "resource_version_expired"
                break

            page += 1
            for item in result.items:
                items.append(item)
                if len(items) >= max_items:
                    if result.continue_token or len(result.items) > 0:
                        truncated = True
                        reason = "item_limit_reached"
                    break

            continue_token = result.continue_token
            if not continue_token:
                break
            if page >= limits.max_pages:
                truncated = True
                reason = "page_limit_reached"
                break

        return items, truncated, reason

    def _build_unavailable_snapshot(
        self,
        target: TargetSelector,
        incident: str,
        *,
        status: CoverageStatus,
        reason: str,
        secrets: tuple[str, ...],
    ) -> EvidenceSnapshot:
        context = {
            "incident": incident,
            "time_window": target.time_window,
            "resources": [],
            "events": [],
            "logs": [],
            "metrics": [],
            "checks": [],
        }
        coverage = {
            source: SourceCoverage(status=status, count=0, reason=reason)
            for source in CANONICAL_EVIDENCE_SOURCES
        }
        return build_snapshot(
            context_alias=target.context_alias,
            namespaces=target.namespaces,
            context=context,
            coverage=coverage,
            source_type="live",
            observed_at=None,
            secrets=secrets,
        )

    def collect(
        self,
        target: TargetSelector,
        limits: CollectionLimits | None = None,
        *,
        incident: str = "Kubernetes incident investigation",
    ) -> EvidenceSnapshot:
        """Collect a bounded, sanitized, scope-hashed EvidenceSnapshot/v1."""
        active_limits = limits or CollectionLimits()
        entry = self._get_target(target)
        safe_incident = redact_text(incident, entry.secrets)

        acquired = entry.semaphore.acquire(timeout=active_limits.deadline_seconds)
        if not acquired:
            if active_limits.allow_cached_fallback:
                cached = self._cache.get_scope_matched(
                    target, incident=safe_incident, secrets=entry.secrets
                )
                if cached is not None:
                    return cached
            return self._build_unavailable_snapshot(
                target,
                safe_incident,
                status="timeout",
                reason="collector_concurrency_timeout",
                secrets=entry.secrets,
            )

        try:
            deadline_at = monotonic() + active_limits.deadline_seconds
            observed_at = utc_now_iso()

            try:
                discovery_timeout = self._remaining_timeout(
                    deadline_at, active_limits.per_request_timeout_seconds
                )
                discovery = entry.k8s_adapter.discover(timeout_seconds=discovery_timeout)
            except AdapterError as error:
                status, reason = _error_to_coverage_status(error)
                if active_limits.allow_cached_fallback:
                    cached = self._cache.get_scope_matched(
                        target, incident=safe_incident, secrets=entry.secrets
                    )
                    if cached is not None:
                        return cached
                return self._build_unavailable_snapshot(
                    target,
                    safe_incident,
                    status=status,
                    reason=reason,
                    secrets=entry.secrets,
                )

            # 1. Resources
            resources: list[dict[str, Any]] = []
            raw_pods: list[dict[str, Any]] = []
            res_truncated = False
            res_errors: list[CoverageStatus] = []
            res_reasons: list[str] = []

            namespaced_specs: list[tuple[str, str, str, bool]] = [
                ("", "v1", "pods", True),
                ("apps", "v1", "deployments", True),
                ("apps", "v1", "replicasets", True),
                ("apps", "v1", "daemonsets", True),
                ("apps", "v1", "statefulsets", True),
                ("", "v1", "services", True),
                ("discovery.k8s.io", "v1", "endpointslices", True),
                ("", "v1", "endpoints", False),
                ("networking.k8s.io", "v1", "networkpolicies", True),
                ("networking.k8s.io", "v1", "ingresses", True),
                ("gateway.networking.k8s.io", "v1", "gateways", True),
                ("gateway.networking.k8s.io", "v1", "httproutes", True),
                ("", "v1", "persistentvolumeclaims", True),
                ("", "v1", "configmaps", True),
                ("", "v1", "serviceaccounts", True),
                ("rbac.authorization.k8s.io", "v1", "roles", True),
                ("rbac.authorization.k8s.io", "v1", "rolebindings", True),
            ]

            for ns in target.namespaces:
                endpointslice_ok = False
                for group, version, plural, apply_selector in namespaced_specs:
                    if plural == "endpoints" and endpointslice_ok:
                        continue
                    if group and not discovery.has_resource(group, plural):
                        continue
                    if len(resources) >= active_limits.max_resources:
                        res_truncated = True
                        break
                    try:
                        raw_items, trunc, rsn = self._list_all_pages(
                            entry.k8s_adapter,
                            group=group,
                            version=version,
                            plural=plural,
                            namespace=ns,
                            label_selector=target.workload_selector if apply_selector else None,
                            limits=active_limits,
                            deadline_at=deadline_at,
                            max_items=active_limits.max_resources - len(resources),
                        )
                        if plural == "endpointslices":
                            endpointslice_ok = True
                        if trunc:
                            res_truncated = True
                            if rsn:
                                res_reasons.append(rsn)
                        for item in raw_items:
                            if plural == "pods":
                                raw_pods.append(item)
                            cleaned = sanitize_resource(item, secrets=entry.secrets)
                            if cleaned is not None:
                                resources.append(cleaned)
                    except AdapterError as error:
                        st, rsn = _error_to_coverage_status(error)
                        if isinstance(error, AdapterNotFoundError):
                            continue
                        res_errors.append(st)
                        res_reasons.append(f"{plural}:{rsn}")
                        if isinstance(error, AdapterTimeoutError):
                            break

            cluster_specs: list[tuple[str, str, str]] = [
                ("storage.k8s.io", "v1", "storageclasses"),
                ("", "v1", "persistentvolumes"),
            ]
            if active_limits.include_csi:
                cluster_specs.extend(
                    [
                        ("storage.k8s.io", "v1", "csidrivers"),
                        ("storage.k8s.io", "v1", "csinodes"),
                    ]
                )
            if active_limits.include_nodes:
                cluster_specs.append(("", "v1", "nodes"))
            if active_limits.include_cluster_rbac:
                cluster_specs.extend(
                    [
                        ("rbac.authorization.k8s.io", "v1", "clusterroles"),
                        ("rbac.authorization.k8s.io", "v1", "clusterrolebindings"),
                    ]
                )

            for group, version, plural in cluster_specs:
                if group and not discovery.has_resource(group, plural):
                    continue
                if len(resources) >= active_limits.max_resources:
                    res_truncated = True
                    break
                try:
                    raw_items, trunc, rsn = self._list_all_pages(
                        entry.k8s_adapter,
                        group=group,
                        version=version,
                        plural=plural,
                        namespace=None,
                        label_selector=None,
                        limits=active_limits,
                        deadline_at=deadline_at,
                        max_items=active_limits.max_resources - len(resources),
                    )
                    if trunc:
                        res_truncated = True
                        if rsn:
                            res_reasons.append(rsn)
                    for item in raw_items:
                        cleaned = sanitize_resource(item, secrets=entry.secrets)
                        if cleaned is not None:
                            resources.append(cleaned)
                except AdapterError as error:
                    if isinstance(error, AdapterNotFoundError):
                        continue
                    st, rsn = _error_to_coverage_status(error)
                    res_errors.append(st)
                    res_reasons.append(f"{plural}:{rsn}")

            coredns_checked = False
            if active_limits.include_coredns and len(resources) < active_limits.max_resources:
                coredns_checked = True
                for group, version, plural in (
                    ("apps", "v1", "deployments"),
                    ("", "v1", "services"),
                    ("discovery.k8s.io", "v1", "endpointslices"),
                    ("", "v1", "configmaps"),
                ):
                    if group and not discovery.has_resource(group, plural):
                        continue
                    if len(resources) >= active_limits.max_resources:
                        res_truncated = True
                        break
                    try:
                        raw_items, _, _ = self._list_all_pages(
                            entry.k8s_adapter,
                            group=group,
                            version=version,
                            plural=plural,
                            namespace="kube-system",
                            label_selector=None,
                            limits=active_limits,
                            deadline_at=deadline_at,
                            max_items=active_limits.max_resources - len(resources),
                        )
                        for item in raw_items:
                            meta = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
                            item_name = str(meta.get("name") or "")
                            labels = meta.get("labels") if isinstance(meta.get("labels"), Mapping) else {}
                            svc_label = str(labels.get("kubernetes.io/service-name") or "")
                            if item_name.startswith(tuple(_COREDNS_NAMES)) or svc_label in _COREDNS_NAMES:
                                cleaned = sanitize_resource(item, secrets=entry.secrets)
                                if cleaned is not None:
                                    resources.append(cleaned)
                    except AdapterError:
                        continue

            if res_truncated:
                res_status: CoverageStatus = "truncated"
            elif resources:
                res_status = _worst_status(res_errors, "observed") if res_errors else "observed"
            elif res_errors:
                res_status = _worst_status(res_errors, "unreachable")
            else:
                res_status = "observed_empty"
            resources_cov = SourceCoverage(
                status=res_status,
                count=len(resources),
                reason="; ".join(res_reasons[:4]) if res_reasons else None,
                truncated=res_truncated,
            )

            # 2. Events
            events: list[dict[str, Any]] = []
            ev_truncated = False
            ev_errors: list[CoverageStatus] = []
            ev_reasons: list[str] = []

            prefer_events_v1 = discovery.has_resource("events.k8s.io", "events")
            for ns in target.namespaces:
                if len(events) >= active_limits.max_events:
                    ev_truncated = True
                    break
                attempts = (
                    [("events.k8s.io", "v1", "events"), ("", "v1", "events")]
                    if prefer_events_v1
                    else [("", "v1", "events")]
                )
                for idx, (group, version, plural) in enumerate(attempts):
                    try:
                        raw_ev, trunc, rsn = self._list_all_pages(
                            entry.k8s_adapter,
                            group=group,
                            version=version,
                            plural=plural,
                            namespace=ns,
                            label_selector=None,
                            limits=active_limits,
                            deadline_at=deadline_at,
                            max_items=active_limits.max_events - len(events),
                        )
                        if trunc:
                            ev_truncated = True
                            if rsn:
                                ev_reasons.append(rsn)
                        events.extend(_normalize_event(item) for item in raw_ev)
                        break
                    except AdapterError as error:
                        if isinstance(error, AdapterNotFoundError) and idx + 1 < len(attempts):
                            continue
                        st, rsn = _error_to_coverage_status(error)
                        ev_errors.append(st)
                        ev_reasons.append(rsn)
                        break

            events = _deduplicate_events(events)[: active_limits.max_events]
            if ev_truncated:
                ev_status: CoverageStatus = "truncated"
            elif events:
                ev_status = "observed"
            elif ev_errors:
                ev_status = _worst_status(ev_errors, "unreachable")
            else:
                ev_status = "observed_empty"
            events_cov = SourceCoverage(
                status=ev_status,
                count=len(events),
                reason="; ".join(ev_reasons[:3]) if ev_reasons else None,
                truncated=ev_truncated,
            )

            # 3. Logs
            logs: list[dict[str, Any]] = []
            log_truncated = False
            log_errors: list[CoverageStatus] = []
            log_reasons: list[str] = []
            total_log_bytes = 0
            sorted_pods = sorted(raw_pods, key=_pod_sort_key)[: active_limits.max_log_pods]

            for pod in sorted_pods:
                if total_log_bytes >= active_limits.max_total_log_bytes:
                    log_truncated = True
                    break
                meta = pod.get("metadata") if isinstance(pod.get("metadata"), Mapping) else {}
                pod_name = str(meta.get("name") or "")
                pod_ns = str(meta.get("namespace") or target.namespaces[0])
                if not pod_name:
                    continue
                for container_name, restarts in _pod_containers(pod):
                    if total_log_bytes >= active_limits.max_total_log_bytes:
                        log_truncated = True
                        break
                    streams = (True, False) if restarts > 0 else (False,)
                    for previous in streams:
                        if total_log_bytes >= active_limits.max_total_log_bytes:
                            log_truncated = True
                            break
                        try:
                            timeout = self._remaining_timeout(
                                deadline_at, active_limits.per_request_timeout_seconds
                            )
                            raw_log = entry.k8s_adapter.read_pod_log(
                                pod_ns,
                                pod_name,
                                container_name,
                                previous=previous,
                                tail_lines=active_limits.max_log_lines_per_container,
                                limit_bytes=active_limits.max_log_bytes_per_container,
                                timeout_seconds=timeout,
                            )
                        except AdapterError as error:
                            if isinstance(error, (AdapterNotFoundError, AdapterInvalidPayloadError)):
                                continue
                            st, rsn = _error_to_coverage_status(error)
                            log_errors.append(st)
                            log_reasons.append(rsn)
                            continue

                        if not raw_log.strip():
                            continue
                        remaining_budget = active_limits.max_total_log_bytes - total_log_bytes
                        encoded = raw_log.encode("utf-8")
                        if len(encoded) > remaining_budget:
                            raw_log = encoded[:remaining_budget].decode("utf-8", errors="ignore")
                            log_truncated = True
                            log_reasons.append("log_byte_limit_reached")
                        elif len(encoded) >= active_limits.max_log_bytes_per_container:
                            log_truncated = True
                            log_reasons.append("container_log_byte_limit_reached")
                        total_log_bytes += len(raw_log.encode("utf-8"))
                        logs.append(
                            {
                                "namespace": pod_ns,
                                "pod": pod_name,
                                "container": container_name,
                                "stream": "previous" if previous else "current",
                                "message": raw_log,
                            }
                        )

            if log_truncated:
                log_status: CoverageStatus = "truncated"
            elif logs:
                log_status = "observed"
            elif log_errors:
                log_status = _worst_status(log_errors, "unreachable")
            elif not raw_pods:
                log_status = "not_requested"
            else:
                log_status = "observed_empty"
            logs_cov = SourceCoverage(
                status=log_status,
                count=len(logs),
                reason="; ".join(dict.fromkeys(log_reasons)) if log_reasons else None,
                truncated=log_truncated,
            )

            # 4. Metrics
            metrics: list[dict[str, Any]] = []
            met_truncated = False
            met_errors: list[CoverageStatus] = []
            met_reasons: list[str] = []
            has_metrics_api = discovery.has_resource("metrics.k8s.io", "pods")

            if has_metrics_api:
                for ns in target.namespaces:
                    if len(metrics) >= active_limits.max_metrics:
                        met_truncated = True
                        break
                    try:
                        raw_pod_metrics, trunc, rsn = self._list_all_pages(
                            entry.k8s_adapter,
                            group="metrics.k8s.io",
                            version="v1beta1",
                            plural="pods",
                            namespace=ns,
                            label_selector=target.workload_selector,
                            limits=active_limits,
                            deadline_at=deadline_at,
                            max_items=active_limits.max_metrics - len(metrics),
                        )
                        if trunc:
                            met_truncated = True
                            if rsn:
                                met_reasons.append(rsn)
                        for item in raw_pod_metrics:
                            meta = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
                            metrics.append(
                                {
                                    "source": "metrics_api",
                                    "kind": "PodMetrics",
                                    "namespace": meta.get("namespace") or ns,
                                    "name": meta.get("name"),
                                    "timestamp": item.get("timestamp"),
                                    "window": item.get("window"),
                                    "containers": item.get("containers") or [],
                                }
                            )
                    except AdapterError as error:
                        st, rsn = _error_to_coverage_status(error)
                        met_errors.append(st)
                        met_reasons.append(rsn)

                if active_limits.include_nodes and len(metrics) < active_limits.max_metrics:
                    try:
                        raw_node_metrics, _, _ = self._list_all_pages(
                            entry.k8s_adapter,
                            group="metrics.k8s.io",
                            version="v1beta1",
                            plural="nodes",
                            namespace=None,
                            label_selector=None,
                            limits=active_limits,
                            deadline_at=deadline_at,
                            max_items=active_limits.max_metrics - len(metrics),
                        )
                        for item in raw_node_metrics:
                            meta = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
                            metrics.append(
                                {
                                    "source": "metrics_api",
                                    "kind": "NodeMetrics",
                                    "name": meta.get("name"),
                                    "timestamp": item.get("timestamp"),
                                    "window": item.get("window"),
                                    "usage": item.get("usage") or {},
                                }
                            )
                    except AdapterError as error:
                        st, rsn = _error_to_coverage_status(error)
                        met_errors.append(st)
                        met_reasons.append(rsn)

            if entry.prometheus_adapter is not None and len(metrics) < active_limits.max_metrics:
                for query_name in _PROMETHEUS_FIXED_QUERIES:
                    if len(metrics) >= active_limits.max_metrics:
                        met_truncated = True
                        break
                    try:
                        timeout = self._remaining_timeout(
                            deadline_at, active_limits.per_request_timeout_seconds
                        )
                        prom_items = entry.prometheus_adapter.query_fixed(
                            query_name,
                            namespaces=tuple(target.namespaces),
                            timeout_seconds=timeout,
                        )
                        remaining_slots = active_limits.max_metrics - len(metrics)
                        if len(prom_items) > remaining_slots:
                            met_truncated = True
                        metrics.extend(prom_items[:remaining_slots])
                    except AdapterError as error:
                        st, rsn = _error_to_coverage_status(error)
                        met_errors.append(st)
                        met_reasons.append(f"prometheus:{rsn}")

            if not has_metrics_api and entry.prometheus_adapter is None:
                met_status: CoverageStatus = "unsupported"
                if not met_reasons:
                    met_reasons.append("metrics_api_unavailable")
            elif met_truncated:
                met_status = "truncated"
            elif metrics:
                met_status = "observed"
            elif met_errors:
                met_status = _worst_status(met_errors, "unreachable")
            else:
                met_status = "observed_empty"
            metrics_cov = SourceCoverage(
                status=met_status,
                count=len(metrics),
                reason="; ".join(dict.fromkeys(met_reasons)) if met_reasons else None,
                truncated=met_truncated,
            )

            # 5. Read-only Checks
            checks = _derive_readonly_checks(resources, coredns_checked=coredns_checked)[
                : active_limits.max_checks
            ]
            if checks:
                chk_status: CoverageStatus = "observed"
                chk_reason: str | None = None
            elif not resources:
                chk_status = "not_requested"
                chk_reason = "no_resources_for_checks"
            else:
                chk_status = "observed_empty"
                chk_reason = None
            checks_cov = SourceCoverage(
                status=chk_status,
                count=len(checks),
                reason=chk_reason,
            )

            context = {
                "incident": safe_incident,
                "time_window": target.time_window,
                "resources": resources,
                "events": events,
                "logs": logs,
                "metrics": metrics,
                "checks": checks,
            }
            coverage = {
                "resources": resources_cov,
                "events": events_cov,
                "logs": logs_cov,
                "metrics": metrics_cov,
                "checks": checks_cov,
            }
            snapshot = build_snapshot(
                context_alias=target.context_alias,
                namespaces=target.namespaces,
                context=context,
                coverage=coverage,
                source_type="live",
                observed_at=observed_at if any(context[s] for s in CANONICAL_EVIDENCE_SOURCES) else None,
                secrets=entry.secrets,
            )

            if snapshot.has_usable_evidence():
                self._cache.put(snapshot)
                return snapshot

            if active_limits.allow_cached_fallback:
                cached = self._cache.get_scope_matched(
                    target, incident=safe_incident, secrets=entry.secrets
                )
                if cached is not None:
                    return cached

            return snapshot
        finally:
            entry.semaphore.release()


def collect(
    target: TargetSelector,
    limits: CollectionLimits | None = None,
    *,
    collector: KubernetesEvidenceCollector,
    incident: str = "Kubernetes incident investigation",
) -> EvidenceSnapshot:
    """Collect a sanitized EvidenceSnapshot for target using the supplied collector."""
    return collector.collect(target, limits, incident=incident)


def build_collector_fn(
    collector: KubernetesEvidenceCollector,
    limits: CollectionLimits | None = None,
) -> CollectorFn:
    """Adapt a KubernetesEvidenceCollector into the RcaApiService CollectorFn contract."""

    def _collect(target: TargetSelector, incident: str) -> EvidenceSnapshot:
        return collector.collect(target, limits, incident=incident)

    return _collect
