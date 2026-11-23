"""Shared runtime contracts for evidence snapshots and RCA runs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import BaseModel, ConfigDict, Field, field_validator

from rca.contracts.schemas import CASE

CoverageStatus = Literal[
    "observed",
    "observed_empty",
    "not_requested",
    "unsupported",
    "forbidden",
    "unreachable",
    "timeout",
    "truncated",
    "invalid",
]
DEGRADED_COVERAGE_STATUSES: frozenset[str] = frozenset(
    {"unsupported", "forbidden", "unreachable", "timeout", "truncated", "invalid"}
)
SnapshotSourceType = Literal["live", "cached", "bundle", "fixture"]
RunStatus = Literal[
    "running",
    "diagnosed",
    "qualified",
    "insufficient_evidence",
    "failed",
    "interrupted",
]
VariantName = Literal["base", "optimal", "mas"]

CANONICAL_EVIDENCE_SOURCES: tuple[str, ...] = (
    "resources",
    "events",
    "logs",
    "metrics",
    "checks",
)
_CONTEXT_VALIDATOR = Draft202012Validator(CASE["properties"]["context"])


def utc_now_iso() -> str:
    """Return the current UTC timestamp formatted in ISO-8601 with Z suffix."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _validate_safe_identifier(value: str, field_name: str, *, allow_star: bool = False) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be empty.")
    forbidden_chars = {"/", "\\", ":"}
    if not allow_star:
        forbidden_chars.add("*")
    if (
        any(ch.isspace() for ch in cleaned)
        or ".." in cleaned
        or any(ch in forbidden_chars for ch in cleaned)
    ):
        raise ValueError(f"{field_name} contains disallowed characters.")
    return cleaned


class TargetSelector(BaseModel):
    """Allowlisted target Kubernetes cluster alias and namespace scope."""

    model_config = ConfigDict(extra="forbid")

    context_alias: str
    namespaces: list[str] = Field(min_length=1)
    workload_selector: str | None = None
    time_window: str | dict[str, Any] | None = None

    @field_validator("context_alias")
    @classmethod
    def _validate_context_alias(cls, value: str) -> str:
        return _validate_safe_identifier(value, "context_alias")

    @field_validator("namespaces")
    @classmethod
    def _validate_namespaces(cls, values: list[str]) -> list[str]:
        cleaned = [_validate_safe_identifier(item, "namespace") for item in values]
        if not cleaned:
            raise ValueError("namespaces must not be empty.")
        return cleaned

    @field_validator("workload_selector")
    @classmethod
    def _validate_workload_selector(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if any(ord(ch) < 32 for ch in cleaned):
            raise ValueError("workload_selector contains control characters.")
        return cleaned


class SourceCoverage(BaseModel):
    """Per-source collection status and count metadata."""

    model_config = ConfigDict(extra="forbid")

    status: CoverageStatus
    count: int = Field(default=0, ge=0)
    reason: str | None = None
    truncated: bool = False


class EvidenceSnapshot(BaseModel):
    """Versioned internal evidence envelope wrapping IncidentContext and coverage."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["v1"] = "v1"
    snapshot_id: str
    scope_hash: str
    source_type: SnapshotSourceType
    context_alias: str | None = None
    namespaces: list[str] = Field(default_factory=list)
    collected_at: str
    observed_at: str | None = None
    context: dict[str, Any]
    coverage: dict[str, SourceCoverage]

    @field_validator("context")
    @classmethod
    def _validate_context(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            _CONTEXT_VALIDATOR.validate(value)
        except JsonSchemaValidationError as error:
            location = ".".join(map(str, error.absolute_path)) or "root"
            raise ValueError(
                f"Invalid IncidentContext at {location} ({error.validator})."
            ) from None
        return value

    def has_usable_evidence(self) -> bool:
        """Return True when at least one evidence source contains items."""
        return any(bool(self.context.get(source)) for source in CANONICAL_EVIDENCE_SOURCES)

    def is_degraded(self) -> bool:
        """Return True when cached fallback or degraded/truncated coverage occurred."""
        if self.source_type == "cached":
            return True
        return any(
            cov.status in DEGRADED_COVERAGE_STATUSES or cov.truncated
            for cov in self.coverage.values()
        )

    def build_limitations(self) -> list[str]:
        """Return deterministic human-readable limitation lines for non-observed sources."""
        lines: list[str] = []
        if self.source_type == "cached":
            lines.append("cached snapshot used after live collection fallback")
        for source in sorted(self.coverage):
            cov = self.coverage[source]
            if cov.status == "observed" and not cov.truncated:
                continue
            details: list[str] = []
            if cov.truncated and cov.status != "truncated":
                details.append("truncated")
            if cov.reason:
                details.append(cov.reason)
            if details:
                lines.append(f"{source}: {cov.status} ({'; '.join(details)})")
            else:
                lines.append(f"{source}: {cov.status}")
        return lines


class RunUsage(BaseModel):
    """Timing and token usage accounting with reported vs unknown call tracking."""

    model_config = ConfigDict(extra="forbid")

    duration_seconds: float | None = None
    logical_calls: int = 0
    reported_input_tokens: int | None = None
    reported_output_tokens: int | None = None
    reported_total_tokens: int | None = None
    unknown_token_calls: int = 0


class RunResult(BaseModel):
    """Canonical API and operator run response envelope."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["v1"] = "v1"
    request_id: str
    run_id: str
    variant: VariantName
    snapshot_id: str | None = None
    snapshot_hash: str | None = None
    status: RunStatus
    coverage: dict[str, SourceCoverage] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)
    result: str | dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    usage: RunUsage = Field(default_factory=RunUsage)
    created_at: str
    completed_at: str | None = None


def compute_scope_hash(
    context_alias: str | None,
    namespaces: list[str],
    context: dict[str, Any],
) -> str:
    """Compute a deterministic SHA-256 hex digest over target scope and context."""
    canonical = json.dumps(
        {
            "context_alias": context_alias,
            "namespaces": namespaces,
            "context": context,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def derive_default_coverage(context: dict[str, Any]) -> dict[str, SourceCoverage]:
    """Derive per-source coverage counts from a canonical IncidentContext."""
    coverage: dict[str, SourceCoverage] = {}
    for source in CANONICAL_EVIDENCE_SOURCES:
        items = context.get(source, [])
        count = len(items) if isinstance(items, list) else 0
        coverage[source] = SourceCoverage(
            status="observed" if count > 0 else "observed_empty",
            count=count,
        )
    return coverage


CollectorFn = Callable[[TargetSelector, str], EvidenceSnapshot]
