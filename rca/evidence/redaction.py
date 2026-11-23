"""Defensive secret redaction and Kubernetes resource sanitization."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

_MAX_CONFIGMAP_VALUE_CHARS = 8192
_MAX_REASON_CHARS = 240

_PEM_BLOCK_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]+PRIVATE KEY-----.*?-----END [A-Z0-9 ]+PRIVATE KEY-----",
    re.DOTALL,
)
_KUBECONFIG_CERT_RE = re.compile(
    r"(?im)\b(client-certificate-data|client-key-data|certificate-authority-data)\s*:\s*(['\"]?)([^\s'\"\n]+)\2"
)
_KUBECONFIG_YAML_CRED_RE = re.compile(
    r"(?im)(^|\n)([ \t]*)(token|password|username)\s*:\s*(['\"]?)([^\s'\"\n]+)\4"
)
_AUTH_HEADER_RE = re.compile(
    r"(?i)\b(Authorization)\s*:\s*(?:(Bearer|Basic|Token)\s+)?([^\s,;'\"]+)"
)
_BEARER_RE = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{6,}")
_TOKEN_PREFIX_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{10,}|gho_[A-Za-z0-9]{10,}|"
    r"github_pat_[A-Za-z0-9_]{10,}|AKIA[0-9A-Z]{16}|"
    r"eyJ[A-Za-z0-9_-]{6,}\.eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{4,})"
)
_URL_CRED_RE = re.compile(r"(https?://)([^/\s:@]+:[^/\s@]+)@")
_KV_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|password|passwd|secret|token|client[_-]?secret|access[_-]?token|refresh[_-]?token)\s*[:=]\s*(['\"]?)([^\s'\",;]+)\2"
)

_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(?:secret|token|password|passwd|api[_-]?key|private[_-]?key|credential|auth|cookie|dsn|tls\.key|client-key)"
)

_SAFE_METADATA_FIELDS = (
    "name",
    "namespace",
    "uid",
    "labels",
    "creationTimestamp",
    "deletionTimestamp",
    "ownerReferences",
    "resourceVersion",
)
_EXCLUDED_ANNOTATIONS = frozenset({"kubectl.kubernetes.io/last-applied-configuration"})
_SAFE_CONTAINER_FIELDS = (
    "name",
    "image",
    "imagePullPolicy",
    "command",
    "args",
    "ports",
    "resources",
    "livenessProbe",
    "readinessProbe",
    "startupProbe",
    "volumeMounts",
    "envFrom",
)
_SAFE_TOP_LEVEL_FIELDS = (
    "apiVersion",
    "kind",
    "status",
    "rules",
    "roleRef",
    "subjects",
    "provisioner",
    "reclaimPolicy",
    "volumeBindingMode",
    "allowVolumeExpansion",
    "subsets",
    "endpoints",
    "addressType",
    "ports",
    "type",
)


def _normalize_secrets(secrets: Iterable[str]) -> tuple[str, ...]:
    unique = {s for s in secrets if isinstance(s, str) and s}
    return tuple(sorted(unique, key=len, reverse=True))


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Scrub explicit secrets, kubeconfig credentials, PEM blocks, and token patterns."""
    cleaned = text
    for secret in _normalize_secrets(secrets):
        cleaned = cleaned.replace(secret, "[REDACTED]")
    cleaned = _PEM_BLOCK_RE.sub("[REDACTED_KEY]", cleaned)
    cleaned = _KUBECONFIG_CERT_RE.sub(r"\1: [REDACTED]", cleaned)
    cleaned = _KUBECONFIG_YAML_CRED_RE.sub(r"\1\2\3: [REDACTED]", cleaned)
    cleaned = _AUTH_HEADER_RE.sub(
        lambda m: f"{m.group(1)}: {m.group(2) + ' ' if m.group(2) else ''}[REDACTED]",
        cleaned,
    )
    cleaned = _BEARER_RE.sub(lambda m: f"{m.group(1)} [REDACTED]", cleaned)
    cleaned = _TOKEN_PREFIX_RE.sub("[REDACTED]", cleaned)
    cleaned = _URL_CRED_RE.sub(r"\1[REDACTED]@", cleaned)
    cleaned = _KV_SECRET_RE.sub(r"\1=[REDACTED]", cleaned)
    return cleaned


def redact_value(value: Any, secrets: Iterable[str] = ()) -> Any:
    """Recursively redact strings inside nested dicts, lists, and tuples."""
    secret_tuple = _normalize_secrets(secrets)
    if isinstance(value, str):
        return redact_text(value, secret_tuple)
    if isinstance(value, list):
        return [redact_value(item, secret_tuple) for item in value]
    if isinstance(value, tuple):
        return [redact_value(item, secret_tuple) for item in value]
    if isinstance(value, Mapping):
        return {
            redact_value(k, secret_tuple): redact_value(v, secret_tuple)
            for k, v in value.items()
        }
    return value


def redact_reason(reason: str | None, secrets: Iterable[str] = ()) -> str | None:
    """Redact and bound a coverage or failure reason string for safe telemetry."""
    if reason is None:
        return None
    cleaned = redact_text(str(reason), secrets).strip()
    if not cleaned:
        return None
    if len(cleaned) > _MAX_REASON_CHARS:
        return cleaned[: _MAX_REASON_CHARS - 3] + "..."
    return cleaned


def _sanitize_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for field in _SAFE_METADATA_FIELDS:
        if field in metadata and metadata[field] is not None:
            sanitized[field] = metadata[field]
    raw_annotations = metadata.get("annotations")
    if isinstance(raw_annotations, Mapping):
        safe_annotations = {
            str(k): v
            for k, v in raw_annotations.items()
            if str(k) not in _EXCLUDED_ANNOTATIONS and not _SENSITIVE_KEY_RE.search(str(k))
        }
        if safe_annotations:
            sanitized["annotations"] = safe_annotations
    return sanitized


def _sanitize_env_list(env_items: Any) -> list[dict[str, Any]]:
    if not isinstance(env_items, list):
        return []
    sanitized: list[dict[str, Any]] = []
    for entry in env_items:
        if not isinstance(entry, Mapping):
            continue
        name = entry.get("name")
        item: dict[str, Any] = {}
        if name is not None:
            item["name"] = str(name)
        if "valueFrom" in entry and entry["valueFrom"] is not None:
            item["valueFrom"] = entry["valueFrom"]
        if "value" in entry:
            if name is not None and _SENSITIVE_KEY_RE.search(str(name)):
                item["value"] = "[REDACTED]"
            else:
                item["value"] = entry["value"]
        sanitized.append(item)
    return sanitized


def _sanitize_container(container: Mapping[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for field in _SAFE_CONTAINER_FIELDS:
        if field in container and container[field] is not None:
            sanitized[field] = container[field]
    if "env" in container and container["env"] is not None:
        sanitized["env"] = _sanitize_env_list(container["env"])
    return sanitized


def _sanitize_pod_spec(pod_spec: Mapping[str, Any]) -> dict[str, Any]:
    sanitized = dict(pod_spec)
    for key in ("containers", "initContainers", "ephemeralContainers"):
        raw_list = pod_spec.get(key)
        if isinstance(raw_list, list):
            sanitized[key] = [
                _sanitize_container(c) for c in raw_list if isinstance(c, Mapping)
            ]
    return sanitized


def _sanitize_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    sanitized = dict(spec)
    if any(k in spec for k in ("containers", "initContainers", "ephemeralContainers")):
        sanitized = _sanitize_pod_spec(sanitized)
    template = spec.get("template")
    if isinstance(template, Mapping):
        template_copy = dict(template)
        if isinstance(template.get("spec"), Mapping):
            template_copy["spec"] = _sanitize_pod_spec(template["spec"])
        sanitized["template"] = template_copy
    job_template = spec.get("jobTemplate")
    if isinstance(job_template, Mapping):
        jt_copy = dict(job_template)
        jt_spec = job_template.get("spec")
        if isinstance(jt_spec, Mapping):
            jt_copy["spec"] = _sanitize_spec(jt_spec)
        sanitized["jobTemplate"] = jt_copy
    return sanitized


def _sanitize_configmap_data(data: Mapping[str, Any]) -> dict[str, str]:
    sanitized: dict[str, str] = {}
    for key, raw_val in data.items():
        key_str = str(key)
        if _SENSITIVE_KEY_RE.search(key_str):
            sanitized[key_str] = "[REDACTED]"
            continue
        text = str(raw_val)
        if len(text) > _MAX_CONFIGMAP_VALUE_CHARS:
            text = text[:_MAX_CONFIGMAP_VALUE_CHARS] + "\n...[truncated]"
        sanitized[key_str] = text
    return sanitized


def sanitize_resource(
    obj: Mapping[str, Any],
    *,
    secrets: Iterable[str] = (),
    allow_secret_key_inventory: bool = False,
) -> dict[str, Any] | None:
    """Return an allowlist-sanitized, secret-redacted Kubernetes resource dictionary.

    Secret objects are dropped by default so secret values never enter model,
    storage, or telemetry boundaries. When ``allow_secret_key_inventory=True``,
    only metadata and sorted key names are retained.
    """
    if not isinstance(obj, Mapping):
        return None
    secret_tuple = _normalize_secrets(secrets)
    kind = obj.get("kind")

    if kind == "Secret":
        if not allow_secret_key_inventory:
            return None
        metadata = obj.get("metadata")
        safe_meta: dict[str, Any] = {}
        if isinstance(metadata, Mapping):
            for field in ("name", "namespace", "creationTimestamp"):
                if field in metadata and metadata[field] is not None:
                    safe_meta[field] = metadata[field]
        raw_data = obj.get("data")
        raw_string_data = obj.get("stringData")
        keys: set[str] = set()
        if isinstance(raw_data, Mapping):
            keys.update(str(k) for k in raw_data)
        if isinstance(raw_string_data, Mapping):
            keys.update(str(k) for k in raw_string_data)
        return redact_value(
            {
                "apiVersion": obj.get("apiVersion", "v1"),
                "kind": "Secret",
                "metadata": safe_meta,
                "type": obj.get("type", "Opaque"),
                "data_keys": sorted(keys),
            },
            secret_tuple,
        )

    sanitized: dict[str, Any] = {}
    for field in _SAFE_TOP_LEVEL_FIELDS:
        if field in obj and obj[field] is not None:
            sanitized[field] = obj[field]

    metadata = obj.get("metadata")
    if isinstance(metadata, Mapping):
        sanitized["metadata"] = _sanitize_metadata(metadata)

    spec = obj.get("spec")
    if isinstance(spec, Mapping):
        sanitized["spec"] = _sanitize_spec(spec)

    if kind == "ConfigMap":
        raw_data = obj.get("data")
        if isinstance(raw_data, Mapping):
            sanitized["data"] = _sanitize_configmap_data(raw_data)
        raw_binary = obj.get("binaryData")
        if isinstance(raw_binary, Mapping):
            sanitized["binaryData"] = {
                str(k): f"<binary:{len(str(v))}>" for k, v in raw_binary.items()
            }

    if kind == "StorageClass" and isinstance(obj.get("parameters"), Mapping):
        sanitized["parameters"] = {
            str(k): "[REDACTED]" if _SENSITIVE_KEY_RE.search(str(k)) else v
            for k, v in obj["parameters"].items()
        }

    return redact_value(sanitized, secret_tuple)
