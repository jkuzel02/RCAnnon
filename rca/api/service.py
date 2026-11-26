"""Bounded synchronous RCA execution and snapshot orchestration for the API."""

from __future__ import annotations

import asyncio
import copy
import threading
import uuid
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from rca.api.config import ApiConfig
from rca.api.models import CreateSnapshotRequest, SnapshotSummary
from rca.api.rendering import redact_text, redact_value
from rca.api.storage import RunStore
from rca.contracts.provider import (
    CompleteFn,
    ProviderError,
    ProviderResponseError,
    ProviderTimeoutError,
)
from rca.contracts.runtime import (
    CANONICAL_EVIDENCE_SOURCES,
    CollectorFn,
    CoverageStatus,
    EvidenceSnapshot,
    RunResult,
    RunUsage,
    SourceCoverage,
    TargetSelector,
    VariantName,
    compute_scope_hash,
    derive_default_coverage,
    utc_now_iso,
)
from rca.providers.openai import OpenAIProvider
from rca.workflows import build_workflow
from rca.workflows.mas_validation import AgentExecutionTrace, MasTracer, _emit_trace


class ScopeDeniedError(Exception):
    """Raised when a target alias or namespace is outside the configured allowlist."""


class SnapshotNotFoundError(Exception):
    """Raised when a requested snapshot does not exist or has expired."""


class RunNotFoundError(Exception):
    """Raised when a requested run ID does not exist."""


class ApiTimeoutError(Exception):
    """Raised when collection or diagnosis exceeds the configured deadline."""


class ApiProviderUnavailableError(Exception):
    """Raised when the model provider fails or returns invalid structured output."""


class RunInterruptedError(Exception):
    """Raised when an active run is cancelled due to client disconnect or shutdown."""


def _epoch_to_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat().replace("+00:00", "Z")


class RcaApiService:
    """Coordinates scoped snapshots, bounded workflow execution, and SQLite persistence."""

    def __init__(
        self,
        config: ApiConfig,
        store: RunStore,
        complete: CompleteFn,
        collector: CollectorFn | None = None,
        tracer: MasTracer | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._complete = complete
        self._collector = collector
        self._tracer = tracer
        self._executor = ThreadPoolExecutor(
            max_workers=config.max_workers,
            thread_name_prefix="rcannon-api",
        )
        secrets = [*config.ui_tokens, *config.operator_tokens, *config.extra_redaction_secrets]
        bound_self = getattr(complete, "__self__", None)
        if isinstance(bound_self, OpenAIProvider):
            secrets.append(bound_self.config.api_key)
        self._secrets: tuple[str, ...] = tuple(s for s in secrets if s)

    @property
    def secrets(self) -> tuple[str, ...]:
        return self._secrets

    def shutdown(self) -> None:
        """Shut down the worker thread pool."""
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _assert_scope_allowed(self, context_alias: str, namespaces: list[str]) -> None:
        if not self._config.is_scope_allowed(context_alias, namespaces):
            raise ScopeDeniedError("Target scope is not authorized by server allowlist.")

    def _build_offline_snapshot(
        self,
        target: TargetSelector,
        incident: str,
        *,
        status: CoverageStatus = "unreachable",
        reason: str = "Target evidence collection is unavailable.",
    ) -> EvidenceSnapshot:
        safe_context: dict[str, Any] = redact_value(
            {
                "incident": incident,
                "time_window": target.time_window,
                "resources": [],
                "events": [],
                "logs": [],
                "metrics": [],
                "checks": [],
            },
            self._secrets,
        )
        coverage = {
            source: SourceCoverage(status=status, count=0, reason=reason)
            for source in CANONICAL_EVIDENCE_SOURCES
        }
        return EvidenceSnapshot(
            snapshot_id=f"snap_{uuid.uuid4().hex}",
            scope_hash=compute_scope_hash(target.context_alias, target.namespaces, safe_context),
            source_type="live",
            context_alias=target.context_alias,
            namespaces=list(target.namespaces),
            collected_at=utc_now_iso(),
            observed_at=None,
            context=safe_context,
            coverage=coverage,
        )

    def _collect_snapshot(self, target: TargetSelector, incident: str) -> EvidenceSnapshot:
        safe_incident = redact_text(incident, self._secrets)
        if self._collector is None:
            return self._build_offline_snapshot(
                target,
                safe_incident,
                status="unreachable",
                reason="Live Kubernetes collector is not configured.",
            )
        try:
            collected = self._collector(target, safe_incident)
        except TimeoutError:
            return self._build_offline_snapshot(
                target,
                safe_incident,
                status="timeout",
                reason="Target collection timed out.",
            )
        except Exception:  # noqa: BLE001
            return self._build_offline_snapshot(
                target,
                safe_incident,
                status="unreachable",
                reason="Target collection failed.",
            )

        redacted_context = redact_value(collected.context, self._secrets)
        redacted_coverage = {
            source: SourceCoverage(
                status=cov.status,
                count=cov.count,
                reason=redact_text(cov.reason, self._secrets) if cov.reason else None,
                truncated=cov.truncated,
            )
            for source, cov in collected.coverage.items()
        }
        snapshot_id = collected.snapshot_id or f"snap_{uuid.uuid4().hex}"
        return EvidenceSnapshot(
            snapshot_id=snapshot_id,
            scope_hash=compute_scope_hash(
                collected.context_alias,
                collected.namespaces,
                redacted_context,
            ),
            source_type=collected.source_type,
            context_alias=collected.context_alias,
            namespaces=list(collected.namespaces),
            collected_at=collected.collected_at or utc_now_iso(),
            observed_at=collected.observed_at,
            context=redacted_context,
            coverage=redacted_coverage,
        )

    def create_snapshot(self, req: CreateSnapshotRequest) -> tuple[EvidenceSnapshot, float]:
        """Validate, redact, and persist an EvidenceSnapshot from a bundle or target."""
        if req.bundle is not None:
            bundle = req.bundle
            if bundle.context_alias is not None:
                if bundle.namespaces:
                    self._assert_scope_allowed(bundle.context_alias, bundle.namespaces)
                elif bundle.context_alias not in self._config.allowed_scopes:
                    raise ScopeDeniedError("Bundle context_alias is not authorized.")
            redacted_context = redact_value(bundle.context, self._secrets)
            coverage = (
                {
                    source: SourceCoverage(
                        status=cov.status,
                        count=cov.count,
                        reason=redact_text(cov.reason, self._secrets) if cov.reason else None,
                        truncated=cov.truncated,
                    )
                    for source, cov in bundle.coverage.items()
                }
                if bundle.coverage is not None
                else derive_default_coverage(redacted_context)
            )
            snapshot = EvidenceSnapshot(
                snapshot_id=f"snap_{uuid.uuid4().hex}",
                scope_hash=compute_scope_hash(
                    bundle.context_alias,
                    bundle.namespaces,
                    redacted_context,
                ),
                source_type=bundle.source_type,
                context_alias=bundle.context_alias,
                namespaces=list(bundle.namespaces),
                collected_at=utc_now_iso(),
                observed_at=bundle.observed_at,
                context=redacted_context,
                coverage=coverage,
            )
        else:
            assert req.target is not None
            assert req.incident is not None
            self._assert_scope_allowed(req.target.context_alias, req.target.namespaces)
            snapshot = self._collect_snapshot(req.target, req.incident)

        expires_at = self._store.put_snapshot(
            snapshot,
            ttl_seconds=self._config.snapshot_ttl_seconds,
            max_snapshots=self._config.max_snapshots,
        )
        return snapshot, expires_at

    async def create_snapshot_async(
        self, req: CreateSnapshotRequest
    ) -> tuple[EvidenceSnapshot, float]:
        """Run create_snapshot off the event loop."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self.create_snapshot, req)

    def to_snapshot_summary(
        self, snapshot: EvidenceSnapshot, expires_at: float
    ) -> SnapshotSummary:
        """Convert a stored EvidenceSnapshot into an operator SnapshotSummary."""
        return SnapshotSummary(
            snapshot_id=snapshot.snapshot_id,
            scope_hash=snapshot.scope_hash,
            source_type=snapshot.source_type,
            context_alias=snapshot.context_alias,
            namespaces=snapshot.namespaces,
            collected_at=snapshot.collected_at,
            observed_at=snapshot.observed_at,
            expires_at=_epoch_to_iso(expires_at),
            coverage=snapshot.coverage,
            has_usable_evidence=snapshot.has_usable_evidence(),
        )

    def get_snapshot_summary(self, snapshot_id: str) -> SnapshotSummary:
        """Retrieve a SnapshotSummary by ID or raise SnapshotNotFoundError."""
        loaded = self._store.get_snapshot(snapshot_id)
        if loaded is None:
            raise SnapshotNotFoundError(f"Snapshot {snapshot_id!r} was not found or expired.")
        return self.to_snapshot_summary(*loaded)

    def get_run(self, run_id: str) -> RunResult:
        """Retrieve a RunResult by ID or raise RunNotFoundError."""
        run = self._store.get_run(run_id)
        if run is None:
            raise RunNotFoundError(f"Run {run_id!r} was not found.")
        return run

    def list_runs(self, *, limit: int = 20) -> list[RunResult]:
        """Return recent RunResult records."""
        return self._store.list_runs(limit=limit)

    def precheck_run_inputs(
        self,
        *,
        snapshot_id: str | None,
        target: TargetSelector | None,
    ) -> EvidenceSnapshot | None:
        """Validate scope and snapshot existence synchronously before starting SSE."""
        if snapshot_id is not None:
            loaded = self._store.get_snapshot(snapshot_id)
            if loaded is None:
                raise SnapshotNotFoundError(
                    f"Snapshot {snapshot_id!r} was not found or expired."
                )
            snapshot, _ = loaded
            if snapshot.context_alias is not None:
                if snapshot.namespaces:
                    self._assert_scope_allowed(snapshot.context_alias, snapshot.namespaces)
                elif snapshot.context_alias not in self._config.allowed_scopes:
                    raise ScopeDeniedError("Snapshot scope is not authorized.")
            return snapshot

        if target is not None:
            self._assert_scope_allowed(target.context_alias, target.namespaces)
            return None

        raise ValueError("Either snapshot_id or target must be provided.")

    async def execute_run(
        self,
        *,
        request_id: str,
        variant: VariantName,
        snapshot_id: str | None = None,
        target: TargetSelector | None = None,
        incident: str | None = None,
        cancel_event: threading.Event | None = None,
        tracer: MasTracer | None = None,
    ) -> RunResult:
        """Execute a bounded RCA workflow run and persist its RunResult envelope."""
        preloaded = self.precheck_run_inputs(snapshot_id=snapshot_id, target=target)
        run_id = f"run_{uuid.uuid4().hex}"
        created_at = utc_now_iso()
        active_cancel = cancel_event or threading.Event()

        initial = RunResult(
            request_id=request_id,
            run_id=run_id,
            variant=variant,
            snapshot_id=preloaded.snapshot_id if preloaded is not None else None,
            snapshot_hash=preloaded.scope_hash if preloaded is not None else None,
            status="running",
            coverage=preloaded.coverage if preloaded is not None else {},
            created_at=created_at,
        )
        self._store.put_run(initial, max_runs=self._config.max_runs)

        started = perf_counter()
        usage_lock = threading.Lock()
        metrics: dict[str, int | None] = {
            "logical_calls": 0,
            "reported_in": None,
            "reported_out": None,
            "reported_tot": None,
            "unknown_token_calls": 0,
        }
        snapshot_holder: dict[str, EvidenceSnapshot] = {}
        if preloaded is not None:
            snapshot_holder["snapshot"] = preloaded
        mas_traces: list[AgentExecutionTrace] = []
        mas_traces_lock = threading.Lock()

        def _record_mas_trace(trace: AgentExecutionTrace) -> None:
            with mas_traces_lock:
                mas_traces.append(trace)
            active_tracer = tracer if tracer is not None else self._tracer
            if active_tracer is not None:
                _emit_trace(active_tracer, trace)

        def tracked_complete(prompt_path: str, payload: Mapping[str, Any]) -> str:
            if active_cancel.is_set():
                raise RunInterruptedError("Run cancelled.")

            bound_self = getattr(self._complete, "__self__", None)
            if isinstance(bound_self, OpenAIProvider):
                resp = bound_self.complete_response(prompt_path, payload)
                text = resp.text
                with usage_lock:
                    metrics["logical_calls"] = (metrics["logical_calls"] or 0) + 1
                    if resp.input_tokens is not None:
                        metrics["reported_in"] = (metrics["reported_in"] or 0) + resp.input_tokens
                    if resp.output_tokens is not None:
                        metrics["reported_out"] = (metrics["reported_out"] or 0) + resp.output_tokens
                    if resp.total_tokens is not None:
                        metrics["reported_tot"] = (metrics["reported_tot"] or 0) + resp.total_tokens
                    if (
                        resp.input_tokens is None
                        or resp.output_tokens is None
                        or resp.total_tokens is None
                    ):
                        metrics["unknown_token_calls"] = (metrics["unknown_token_calls"] or 0) + 1
            else:
                text = self._complete(prompt_path, payload)
                with usage_lock:
                    metrics["logical_calls"] = (metrics["logical_calls"] or 0) + 1
                    metrics["unknown_token_calls"] = (metrics["unknown_token_calls"] or 0) + 1

            if active_cancel.is_set():
                raise RunInterruptedError("Run cancelled.")
            return text

        def _worker() -> RunResult:
            if active_cancel.is_set():
                raise RunInterruptedError("Run cancelled.")

            if "snapshot" not in snapshot_holder:
                assert target is not None
                assert incident is not None
                collected = self._collect_snapshot(target, incident)
                self._store.put_snapshot(
                    collected,
                    ttl_seconds=self._config.snapshot_ttl_seconds,
                    max_snapshots=self._config.max_snapshots,
                )
                snapshot_holder["snapshot"] = collected

            snapshot = snapshot_holder["snapshot"]
            limitations = snapshot.build_limitations()

            if not snapshot.has_usable_evidence():
                return RunResult(
                    request_id=request_id,
                    run_id=run_id,
                    variant=variant,
                    snapshot_id=snapshot.snapshot_id,
                    snapshot_hash=snapshot.scope_hash,
                    status="insufficient_evidence",
                    coverage=snapshot.coverage,
                    limitations=limitations or ["No usable Kubernetes evidence was collected."],
                    result=None,
                    usage=RunUsage(
                        duration_seconds=round(perf_counter() - started, 4),
                        logical_calls=0,
                    ),
                    created_at=created_at,
                    completed_at=utc_now_iso(),
                )

            working_context = copy.deepcopy(snapshot.context)
            if incident is not None and preloaded is not None:
                working_context["incident"] = redact_text(incident, self._secrets)

            if variant == "mas":
                graph = build_workflow(
                    variant,
                    complete=tracked_complete,
                    validate_evidence=True,
                    coverage=snapshot.coverage,
                    tracer=_record_mas_trace,
                    secrets=self._secrets,
                )
            else:
                graph = build_workflow(variant, complete=tracked_complete)
            raw_output = graph.invoke({"context": working_context})
            redacted_result = redact_value(raw_output["root-cause"], self._secrets)
            final_status = "qualified" if snapshot.is_degraded() else "diagnosed"

            return RunResult(
                request_id=request_id,
                run_id=run_id,
                variant=variant,
                snapshot_id=snapshot.snapshot_id,
                snapshot_hash=snapshot.scope_hash,
                status=final_status,
                coverage=snapshot.coverage,
                limitations=limitations,
                result=redacted_result,
                usage=RunUsage(
                    duration_seconds=round(perf_counter() - started, 4),
                    logical_calls=metrics["logical_calls"] or 0,
                    reported_input_tokens=metrics["reported_in"],
                    reported_output_tokens=metrics["reported_out"],
                    reported_total_tokens=metrics["reported_tot"],
                    unknown_token_calls=metrics["unknown_token_calls"] or 0,
                ),
                created_at=created_at,
                completed_at=utc_now_iso(),
            )

        def _build_usage() -> RunUsage:
            return RunUsage(
                duration_seconds=round(perf_counter() - started, 4),
                logical_calls=metrics["logical_calls"] or 0,
                reported_input_tokens=metrics["reported_in"],
                reported_output_tokens=metrics["reported_out"],
                reported_total_tokens=metrics["reported_tot"],
                unknown_token_calls=metrics["unknown_token_calls"] or 0,
            )

        def _terminal_snapshot_fields() -> tuple[str | None, str | None, dict[str, SourceCoverage]]:
            snap = snapshot_holder.get("snapshot")
            if snap is None:
                return None, None, {}
            return snap.snapshot_id, snap.scope_hash, snap.coverage

        def _terminal_limitations() -> list[str]:
            snap = snapshot_holder.get("snapshot")
            lines = snap.build_limitations() if snap is not None else []
            with mas_traces_lock:
                traces = list(mas_traces)
            for trace in traces:
                if trace.status in {"failed", "cancelled"} and trace.error_category:
                    line = redact_text(
                        f"mas/{trace.agent}: {trace.status} ({trace.error_category})",
                        self._secrets,
                    )
                    if line and line not in lines:
                        lines.append(line)
            return lines

        loop = asyncio.get_running_loop()
        try:
            finalized = await asyncio.wait_for(
                loop.run_in_executor(self._executor, _worker),
                timeout=self._config.run_deadline_seconds,
            )
            self._store.update_run(finalized)
            return finalized
        except RunInterruptedError:
            active_cancel.set()
            snap_id, snap_hash, cov = _terminal_snapshot_fields()
            interrupted = RunResult(
                request_id=request_id,
                run_id=run_id,
                variant=variant,
                snapshot_id=snap_id,
                snapshot_hash=snap_hash,
                status="interrupted",
                coverage=cov,
                limitations=_terminal_limitations(),
                error_code="client_disconnected",
                error_message="Run interrupted before completion.",
                usage=_build_usage(),
                created_at=created_at,
                completed_at=utc_now_iso(),
            )
            self._store.update_run(interrupted)
            return interrupted
        except asyncio.CancelledError:
            active_cancel.set()
            snap_id, snap_hash, cov = _terminal_snapshot_fields()
            interrupted = RunResult(
                request_id=request_id,
                run_id=run_id,
                variant=variant,
                snapshot_id=snap_id,
                snapshot_hash=snap_hash,
                status="interrupted",
                coverage=cov,
                limitations=_terminal_limitations(),
                error_code="client_disconnected",
                error_message="Run interrupted before completion.",
                usage=_build_usage(),
                created_at=created_at,
                completed_at=utc_now_iso(),
            )
            self._store.update_run(interrupted)
            raise
        except (TimeoutError, asyncio.TimeoutError, ProviderTimeoutError):
            active_cancel.set()
            snap_id, snap_hash, cov = _terminal_snapshot_fields()
            failed = RunResult(
                request_id=request_id,
                run_id=run_id,
                variant=variant,
                snapshot_id=snap_id,
                snapshot_hash=snap_hash,
                status="failed",
                coverage=cov,
                limitations=_terminal_limitations(),
                error_code="timeout",
                error_message="Diagnosis timed out before completion.",
                usage=_build_usage(),
                created_at=created_at,
                completed_at=utc_now_iso(),
            )
            self._store.update_run(failed)
            raise ApiTimeoutError("Diagnosis timed out before completion.") from None
        except (ProviderResponseError, ValueError):
            snap_id, snap_hash, cov = _terminal_snapshot_fields()
            failed = RunResult(
                request_id=request_id,
                run_id=run_id,
                variant=variant,
                snapshot_id=snap_id,
                snapshot_hash=snap_hash,
                status="failed",
                coverage=cov,
                limitations=_terminal_limitations(),
                error_code="invalid_model_output",
                error_message="Model returned an invalid structured response.",
                usage=_build_usage(),
                created_at=created_at,
                completed_at=utc_now_iso(),
            )
            self._store.update_run(failed)
            raise ApiProviderUnavailableError(
                "Model returned an invalid structured response."
            ) from None
        except (ProviderError, Exception):  # noqa: BLE001
            snap_id, snap_hash, cov = _terminal_snapshot_fields()
            failed = RunResult(
                request_id=request_id,
                run_id=run_id,
                variant=variant,
                snapshot_id=snap_id,
                snapshot_hash=snap_hash,
                status="failed",
                coverage=cov,
                limitations=_terminal_limitations(),
                error_code="provider_unavailable",
                error_message="Model provider is currently unavailable.",
                usage=_build_usage(),
                created_at=created_at,
                completed_at=utc_now_iso(),
            )
            self._store.update_run(failed)
            raise ApiProviderUnavailableError(
                "Model provider is currently unavailable."
            ) from None
