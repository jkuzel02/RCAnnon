"""Evidence snapshot builder, collection bounds, and scope-matched cache."""

from __future__ import annotations

import copy
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from rca.contracts.runtime import (
    CANONICAL_EVIDENCE_SOURCES,
    CoverageStatus,
    EvidenceSnapshot,
    SnapshotSourceType,
    SourceCoverage,
    TargetSelector,
    compute_scope_hash,
    utc_now_iso,
)
from rca.evidence.redaction import redact_reason, redact_text, redact_value

DEFAULT_MAX_BUNDLE_BYTES = 256 * 1024


@dataclass(frozen=True)
class CollectionLimits:
    """Bounded timeouts, concurrency, page, and payload limits for collection."""

    deadline_seconds: float = 30.0
    per_request_timeout_seconds: float = 10.0
    max_concurrency: int = 4
    page_size: int = 100
    max_pages: int = 5
    max_resources: int = 200
    max_events: int = 200
    max_log_pods: int = 10
    max_log_lines_per_container: int = 200
    max_log_bytes_per_container: int = 32_768
    max_total_log_bytes: int = 131_072
    max_metrics: int = 200
    max_checks: int = 50
    max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES
    include_nodes: bool = False
    include_cluster_rbac: bool = False
    include_coredns: bool = True
    include_csi: bool = True
    allow_cached_fallback: bool = True

    def __post_init__(self) -> None:
        positive_floats = (
            ("deadline_seconds", self.deadline_seconds),
            ("per_request_timeout_seconds", self.per_request_timeout_seconds),
        )
        for name, val in positive_floats:
            if val <= 0:
                raise ValueError(f"{name} must be greater than zero.")
        positive_ints = (
            ("max_concurrency", self.max_concurrency),
            ("page_size", self.page_size),
            ("max_pages", self.max_pages),
            ("max_resources", self.max_resources),
            ("max_events", self.max_events),
            ("max_log_pods", self.max_log_pods),
            ("max_log_lines_per_container", self.max_log_lines_per_container),
            ("max_log_bytes_per_container", self.max_log_bytes_per_container),
            ("max_total_log_bytes", self.max_total_log_bytes),
            ("max_metrics", self.max_metrics),
            ("max_checks", self.max_checks),
            ("max_bundle_bytes", self.max_bundle_bytes),
        )
        for name, val in positive_ints:
            if val <= 0:
                raise ValueError(f"{name} must be greater than zero.")


def normalize_coverage(
    coverage: Mapping[str, SourceCoverage],
    context: Mapping[str, Any],
    *,
    secrets: Iterable[str] = (),
) -> dict[str, SourceCoverage]:
    """Normalize per-source coverage counts and enforce non-empty observed semantics."""
    normalized: dict[str, SourceCoverage] = {}
    for source in CANONICAL_EVIDENCE_SOURCES:
        items = context.get(source, [])
        count = len(items) if isinstance(items, list) else 0
        existing = coverage.get(source)
        if existing is None:
            status: CoverageStatus = "observed" if count > 0 else "observed_empty"
            normalized[source] = SourceCoverage(status=status, count=count)
            continue

        status = existing.status
        if status == "observed" and count == 0:
            status = "observed_empty"
        normalized[source] = SourceCoverage(
            status=status,
            count=count,
            reason=redact_reason(existing.reason, secrets),
            truncated=existing.truncated,
        )
    return normalized


def build_snapshot(
    *,
    context_alias: str | None,
    namespaces: Iterable[str],
    context: Mapping[str, Any],
    coverage: Mapping[str, SourceCoverage],
    source_type: SnapshotSourceType = "live",
    collected_at: str | None = None,
    observed_at: str | None = None,
    snapshot_id: str | None = None,
    secrets: Iterable[str] = (),
) -> EvidenceSnapshot:
    """Build a redacted, scope-hashed EvidenceSnapshot/v1 envelope."""
    ns_list = list(namespaces)
    redacted_context: dict[str, Any] = redact_value(dict(context), secrets)
    normalized_cov = normalize_coverage(coverage, redacted_context, secrets=secrets)
    scope_hash = compute_scope_hash(context_alias, ns_list, redacted_context)
    return EvidenceSnapshot(
        snapshot_id=snapshot_id or f"snap_{uuid.uuid4().hex}",
        scope_hash=scope_hash,
        source_type=source_type,
        context_alias=context_alias,
        namespaces=ns_list,
        collected_at=collected_at or utc_now_iso(),
        observed_at=observed_at,
        context=redacted_context,
        coverage=normalized_cov,
    )


class SnapshotCache:
    """Thread-safe bounded cache of usable snapshots keyed by target scope."""

    def __init__(
        self,
        *,
        max_entries: int = 64,
        ttl_seconds: float = 3600.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be greater than zero.")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than zero.")
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple[str, tuple[str, ...]], tuple[float, EvidenceSnapshot]] = (
            OrderedDict()
        )

    @staticmethod
    def _scope_key(context_alias: str, namespaces: Iterable[str]) -> tuple[str, tuple[str, ...]]:
        return (context_alias, tuple(sorted(namespaces)))

    def _evict_expired(self, now: float) -> None:
        expired = [k for k, (expires_at, _) in self._entries.items() if expires_at <= now]
        for key in expired:
            del self._entries[key]

    def put(self, snapshot: EvidenceSnapshot) -> bool:
        """Retain a usable scoped snapshot; return True if stored."""
        if snapshot.context_alias is None or not snapshot.namespaces:
            return False
        if not snapshot.has_usable_evidence():
            return False
        key = self._scope_key(snapshot.context_alias, snapshot.namespaces)
        now = self._clock()
        stored = copy.deepcopy(snapshot)
        with self._lock:
            self._evict_expired(now)
            if key in self._entries:
                del self._entries[key]
            self._entries[key] = (now + self._ttl_seconds, stored)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
        return True

    def get_scope_matched(
        self,
        target: TargetSelector,
        *,
        incident: str | None = None,
        secrets: Iterable[str] = (),
    ) -> EvidenceSnapshot | None:
        """Return a cached copy matching target alias and namespaces, if valid."""
        key = self._scope_key(target.context_alias, target.namespaces)
        now = self._clock()
        with self._lock:
            self._evict_expired(now)
            item = self._entries.get(key)
            if item is None:
                return None
            self._entries.move_to_end(key)
            cached = copy.deepcopy(item[1])

        context = copy.deepcopy(cached.context)
        if incident is not None:
            context["incident"] = redact_text(incident, secrets)
        return build_snapshot(
            context_alias=target.context_alias,
            namespaces=list(target.namespaces),
            context=context,
            coverage=cached.coverage,
            source_type="cached",
            collected_at=utc_now_iso(),
            observed_at=cached.observed_at or cached.collected_at,
            secrets=secrets,
        )
