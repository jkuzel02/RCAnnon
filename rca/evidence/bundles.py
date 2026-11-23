"""Bounded JSON bundle import and export with schema and scope validation."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

from rca.contracts.runtime import (
    CANONICAL_EVIDENCE_SOURCES,
    EvidenceSnapshot,
    SnapshotSourceType,
    SourceCoverage,
)
from rca.contracts.schemas import CASE
from rca.evidence.redaction import redact_reason, redact_value, sanitize_resource
from rca.evidence.snapshot import CollectionLimits, build_snapshot

_CONTEXT_VALIDATOR = Draft202012Validator(CASE["properties"]["context"])
_ALLOWED_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "snapshot_id",
        "scope_hash",
        "source_type",
        "context_alias",
        "namespaces",
        "collected_at",
        "observed_at",
        "context",
        "coverage",
    }
)
_ALLOWED_SOURCE_TYPES = frozenset({"live", "cached", "bundle", "fixture"})


class BundleValidationError(ValueError):
    """Raised when an imported evidence bundle fails structural or safety checks."""


class BundleTooLargeError(BundleValidationError):
    """Raised when an imported evidence bundle exceeds the configured byte cap."""


class BundleScopeDeniedError(PermissionError):
    """Raised when an imported bundle targets a cluster alias or namespace outside allowlist."""


def _read_bundle_bytes(source: bytes | str | Path, max_bytes: int) -> bytes:
    if isinstance(source, Path):
        if source.is_symlink():
            raise BundleValidationError("Symlink bundle paths are not allowed.")
        if not source.is_file():
            raise BundleValidationError("Bundle path must point to a regular JSON file.")
        if source.suffix.lower() != ".json":
            raise BundleValidationError("Only plain .json evidence bundles are supported.")
        size = source.stat().st_size
        if size > max_bytes:
            raise BundleTooLargeError(
                f"Bundle size ({size} bytes) exceeds maximum allowed ({max_bytes} bytes)."
            )
        raw = source.read_bytes()
    elif isinstance(source, bytes):
        raw = source
    elif isinstance(source, str):
        raw = source.encode("utf-8")
    else:
        raise BundleValidationError("Bundle source must be bytes, str, or Path.")

    if len(raw) > max_bytes:
        raise BundleTooLargeError(
            f"Bundle size ({len(raw)} bytes) exceeds maximum allowed ({max_bytes} bytes)."
        )
    return raw


def _cap_list(items: list[Any], limit: int) -> tuple[list[Any], bool]:
    if len(items) <= limit:
        return items, False
    return items[:limit], True


def _cap_logs(logs: list[Any], max_bytes: int) -> tuple[list[Any], bool]:
    kept: list[Any] = []
    used_bytes = 0
    for entry in logs:
        encoded_len = len(json.dumps(entry, ensure_ascii=False).encode("utf-8"))
        if used_bytes + encoded_len > max_bytes:
            return kept, True
        kept.append(entry)
        used_bytes += encoded_len
    return kept, False


def import_bundle(
    source: bytes | str | Path,
    *,
    limits: CollectionLimits | None = None,
    allowed_scopes: Mapping[str, frozenset[str]] | None = None,
    extra_secrets: Iterable[str] = (),
) -> EvidenceSnapshot:
    """Validate, sanitize, bound, and import a JSON evidence bundle or fixture."""
    active_limits = limits or CollectionLimits()
    raw_bytes = _read_bundle_bytes(source, active_limits.max_bundle_bytes)

    try:
        parsed = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BundleValidationError("Evidence bundle must be valid UTF-8 JSON.") from error

    if not isinstance(parsed, dict):
        raise BundleValidationError("Evidence bundle root must be a JSON object.")

    if "root-cause" in parsed:
        raise BundleValidationError("Evaluator-only 'root-cause' field is not permitted in bundles.")

    unknown_keys = sorted(set(parsed) - _ALLOWED_TOP_LEVEL_KEYS)
    if unknown_keys:
        raise BundleValidationError(
            f"Unsupported bundle top-level field(s): {', '.join(unknown_keys)}."
        )

    schema_version = parsed.get("schema_version", "v1")
    if schema_version != "v1":
        raise BundleValidationError(f"Unsupported bundle schema_version: {schema_version!r}.")

    raw_context = parsed.get("context")
    if not isinstance(raw_context, dict):
        raise BundleValidationError("Evidence bundle must contain a 'context' object.")
    if "root-cause" in raw_context:
        raise BundleValidationError("Evaluator-only 'root-cause' field is not permitted in context.")

    try:
        _CONTEXT_VALIDATOR.validate(raw_context)
    except JsonSchemaValidationError as error:
        location = ".".join(map(str, error.absolute_path)) or "root"
        raise BundleValidationError(
            f"Invalid IncidentContext at {location} ({error.validator})."
        ) from None

    context_alias = parsed.get("context_alias")
    if context_alias is not None and not isinstance(context_alias, str):
        raise BundleValidationError("context_alias must be a string or null.")

    raw_namespaces = parsed.get("namespaces", [])
    if not isinstance(raw_namespaces, list) or not all(
        isinstance(ns, str) and ns.strip() for ns in raw_namespaces
    ):
        raise BundleValidationError("namespaces must be a list of non-empty strings.")
    namespaces = [ns.strip() for ns in raw_namespaces]

    if allowed_scopes is not None and context_alias is not None:
        allowed_ns = allowed_scopes.get(context_alias)
        if allowed_ns is None or (namespaces and not all(ns in allowed_ns for ns in namespaces)):
            raise BundleScopeDeniedError("Bundle scope is outside the configured allowlist.")

    raw_source_type = parsed.get("source_type", "bundle")
    if raw_source_type not in _ALLOWED_SOURCE_TYPES:
        raise BundleValidationError(f"Unsupported source_type: {raw_source_type!r}.")
    source_type: SnapshotSourceType = "fixture" if raw_source_type == "fixture" else "bundle"

    observed_at = parsed.get("observed_at")
    if observed_at is not None and not isinstance(observed_at, str):
        raise BundleValidationError("observed_at must be an ISO-8601 string or null.")
    collected_at = parsed.get("collected_at")
    if collected_at is not None and not isinstance(collected_at, str):
        raise BundleValidationError("collected_at must be an ISO-8601 string or null.")

    raw_coverage = parsed.get("coverage")
    parsed_coverage: dict[str, SourceCoverage] = {}
    if raw_coverage is not None:
        if not isinstance(raw_coverage, dict):
            raise BundleValidationError("coverage must be an object when provided.")
        for source_name, cov_payload in raw_coverage.items():
            if source_name not in CANONICAL_EVIDENCE_SOURCES:
                raise BundleValidationError(f"Unknown coverage source: {source_name!r}.")
            try:
                parsed_coverage[source_name] = SourceCoverage.model_validate(cov_payload)
            except Exception as error:  # noqa: BLE001
                raise BundleValidationError(
                    f"Invalid coverage entry for {source_name!r}."
                ) from error

    context = copy.deepcopy(raw_context)
    sanitized_resources: list[dict[str, Any]] = []
    for item in context["resources"]:
        cleaned = sanitize_resource(item, secrets=extra_secrets)
        if cleaned is not None:
            sanitized_resources.append(cleaned)

    resources, res_truncated = _cap_list(sanitized_resources, active_limits.max_resources)
    events, ev_truncated = _cap_list(list(context["events"]), active_limits.max_events)
    logs, log_truncated = _cap_logs(list(context["logs"]), active_limits.max_total_log_bytes)
    metrics, met_truncated = _cap_list(list(context["metrics"]), active_limits.max_metrics)
    checks, chk_truncated = _cap_list(list(context["checks"]), active_limits.max_checks)

    bounded_context = {
        "incident": redact_value(context["incident"], extra_secrets),
        "time_window": redact_value(context["time_window"], extra_secrets),
        "resources": resources,
        "events": events,
        "logs": logs,
        "metrics": metrics,
        "checks": checks,
    }

    truncation_flags = {
        "resources": (res_truncated, "bundle_item_limit_reached"),
        "events": (ev_truncated, "bundle_item_limit_reached"),
        "logs": (log_truncated, "bundle_log_byte_limit_reached"),
        "metrics": (met_truncated, "bundle_item_limit_reached"),
        "checks": (chk_truncated, "bundle_item_limit_reached"),
    }

    coverage: dict[str, SourceCoverage] = {}
    for source_name in CANONICAL_EVIDENCE_SOURCES:
        items = bounded_context[source_name]
        existing = parsed_coverage.get(source_name)
        was_truncated, trunc_reason = truncation_flags[source_name]
        if existing is not None:
            status = "truncated" if was_truncated else existing.status
            reason = (
                trunc_reason
                if was_truncated and not existing.reason
                else redact_reason(existing.reason, extra_secrets)
            )
            coverage[source_name] = SourceCoverage(
                status=status,
                count=len(items),
                reason=reason,
                truncated=existing.truncated or was_truncated,
            )
        else:
            coverage[source_name] = SourceCoverage(
                status="truncated" if was_truncated else ("observed" if items else "observed_empty"),
                count=len(items),
                reason=trunc_reason if was_truncated else None,
                truncated=was_truncated,
            )

    return build_snapshot(
        context_alias=context_alias,
        namespaces=namespaces,
        context=bounded_context,
        coverage=coverage,
        source_type=source_type,
        collected_at=collected_at,
        observed_at=observed_at,
        secrets=extra_secrets,
    )


def export_bundle(
    snapshot: EvidenceSnapshot,
    *,
    extra_secrets: Iterable[str] = (),
) -> str:
    """Serialize a sanitized EvidenceSnapshot to a defensive redacted JSON string."""
    payload = snapshot.model_dump()
    payload["context"] = redact_value(payload["context"], extra_secrets)
    for cov in payload["coverage"].values():
        if cov.get("reason"):
            cov["reason"] = redact_reason(cov["reason"], extra_secrets)
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
