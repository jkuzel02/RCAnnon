"""Evidence claim validation, reference verification, and payload-free tracing for MAS."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph

from rca.contracts.provider import (
    CompleteFn,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
)
from rca.contracts.runtime import SourceCoverage
from rca.contracts.schemas import (
    RCA,
    REPORT_SCHEMAS,
    parse_and_validate_json,
    validate_observations,
)
from rca.evidence.redaction import redact_reason

MAX_SPECIALIST_CONCURRENCY = 3
COORDINATOR_PROMPT_PATH = "mas/coordinator.md"
SPECIALIST_ORDER: tuple[str, ...] = (
    "k8s-config-events",
    "logs-connectivity",
    "metrics-telemetry",
)
SPECIALIST_STATE_KEYS: dict[str, str] = {
    "k8s-config-events": "k8s_config_events_report",
    "logs-connectivity": "logs_connectivity_report",
    "metrics-telemetry": "metrics_telemetry_report",
}
SPECIALIST_PROMPTS: dict[str, str] = {
    name: f"mas/{name}.md" for name in SPECIALIST_ORDER
}

_CLAUSE_SPLIT_RE = re.compile(
    r"[.;\n]+|,\s*(?=(?:although|while|whereas|but|however|though)\b)",
    re.IGNORECASE,
)
_ABSENCE_OR_HEDGE_RE = re.compile(
    r"\b(?:no|not|none|never|without|absent|missing|uncollected|unobserved|"
    r"unavailable|unmeasured|unrecorded|undetermined|empty|cannot|could\s+not|"
    r"unable\s+to|neither|nor|lack|lacks|lacking|omitted|skipped|unknown|"
    r"failed\s+to\s+collect|not\s+evidenced|unverified|if\s+collected|"
    r"when\s+collected|pending\s+collection|would\s+confirm|expected)\b",
    re.IGNORECASE,
)
_AFFIRMATIVE_SOURCE_PATTERNS: dict[str, re.Pattern[str]] = {
    "logs": re.compile(
        r"\b(?:(?:container|pod|application|previous)\s+logs?|logs?|stdout|stderr)\s+"
        r"(?:show|shows|showed|confirm|confirms|confirmed|indicate|indicates|indicated|"
        r"report|reports|reported|reveal|reveals|revealed|record|records|recorded|"
        r"contain|contains|contained|demonstrate|demonstrates|logged)\b"
        r"|\b(?:application|container|pod|service|worker)\s+logged\b",
        re.IGNORECASE,
    ),
    "events": re.compile(
        r"\b(?:(?:kubernetes|cluster|kubelet|scheduler)\s+events?|events?)\s+"
        r"(?:show|shows|showed|confirm|confirms|confirmed|indicate|indicates|indicated|"
        r"report|reports|reported|record|records|recorded|contain|contains|contained|"
        r"observed)\b"
        r"|\b(?:oomkilling|failedscheduling|unhealthy|killing|backoff|errimagepull)\s+"
        r"events?\s+(?:occurred|observed|recorded|triggered)\b",
        re.IGNORECASE,
    ),
    "metrics": re.compile(
        r"\b(?:(?:prometheus|time[- ]series)\s+metrics?|metrics?|telemetry|"
        r"memory\s+samples?|cpu\s+samples?)\s+"
        r"(?:show|shows|showed|confirm|confirms|confirmed|indicate|indicates|indicated|"
        r"report|reports|reported|measured|recorded|reached|spiked|peaked)\b"
        r"|\b(?:memory\s+working\s+set|cpu\s+throttling|cfs\s+throttling)\s+"
        r"(?:reached|spiked|peaked|exceeded|observed|detected|measured)\b",
        re.IGNORECASE,
    ),
    "checks": re.compile(
        r"\b(?:dns\s+(?:lookup|check)|tcp\s+(?:connect|check)|http\s+(?:get|check)|"
        r"connectivity\s+check|probe\s+check|direct\s+pod(?:-ip)?\s+check|"
        r"endpoint\s+check|authorization\s+check)\s+"
        r"(?:succeeded|failed|passed|returned|timed\s+out|confirmed|showed)\b",
        re.IGNORECASE,
    ),
}

_K8S_REF_RE = re.compile(
    r"\b(?P<kind>Deployment|Service|Pod|ConfigMap|Secret|NetworkPolicy|"
    r"PersistentVolumeClaim|PVC|StorageClass|Role|RoleBinding|ClusterRole|"
    r"ClusterRoleBinding|ServiceAccount|Ingress|Gateway|HTTPRoute|DaemonSet|"
    r"StatefulSet|ReplicaSet|Job|CronJob|Node)/(?P<name>[A-Za-z0-9._-]+)\b"
)
_KIND_ALIASES: dict[str, str] = {"PVC": "PersistentVolumeClaim"}
_POD_CONTROLLER_KINDS: frozenset[str] = frozenset(
    {"Deployment", "ReplicaSet", "StatefulSet", "DaemonSet", "Job", "CronJob"}
)
_DEGRADED_OR_EMPTY_STATUSES: frozenset[str] = frozenset(
    {
        "observed_empty",
        "not_requested",
        "unsupported",
        "forbidden",
        "unreachable",
        "timeout",
        "invalid",
    }
)


@dataclass(frozen=True)
class AgentExecutionTrace:
    """Payload-free per-agent execution timing and error metadata."""

    agent: str
    status: Literal["passed", "failed", "blocked", "cancelled"]
    duration_seconds: float
    error_category: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class EvidenceClaimIssue:
    """Single machine-checked evidence claim or reference violation."""

    source: str
    field: str
    category: Literal[
        "unsupported_observation",
        "unverified_reference",
        "contradictory_finding",
    ]
    detail: str


@dataclass(frozen=True)
class KnownContextReferences:
    """Stable Kubernetes and telemetry identifiers extracted from IncidentContext."""

    has_resources: bool
    resource_refs: frozenset[str]
    controller_names: frozenset[str]
    pod_names: frozenset[str]
    container_names: frozenset[str]
    log_sources: frozenset[str]
    metric_names: frozenset[str]
    check_targets: frozenset[str]

    def knows_ref(self, kind: str, name: str) -> bool:
        canonical_kind = _KIND_ALIASES.get(kind, kind)
        ref = f"{canonical_kind}/{name}"
        if ref in self.resource_refs:
            return True
        if canonical_kind == "Pod":
            if name in self.pod_names:
                return True
            if any(
                name == ctrl or name.startswith(f"{ctrl}-")
                for ctrl in self.controller_names
            ):
                return True
        return False


class MasValidationError(ValueError):
    """Raised when MAS specialist or coordinator output fails validation."""

    def __init__(
        self,
        message: str,
        *,
        agent: str,
        error_category: str,
        issues: Iterable[EvidenceClaimIssue] = (),
    ) -> None:
        super().__init__(message)
        self.agent = agent
        self.error_category = error_category
        self.issues: tuple[EvidenceClaimIssue, ...] = tuple(issues)


MasTracer = Callable[[AgentExecutionTrace], None] | Any


class RecordingMasTracer:
    """Thread-safe MasTracer collecting payload-free AgentExecutionTrace records."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: list[AgentExecutionTrace] = []

    def __call__(self, record: AgentExecutionTrace) -> None:
        with self._lock:
            self._records.append(record)

    @property
    def records(self) -> list[AgentExecutionTrace]:
        with self._lock:
            return list(self._records)


def _emit_trace(tracer: MasTracer | None, record: AgentExecutionTrace) -> None:
    if tracer is None:
        return
    record_fn = getattr(tracer, "record", None)
    if callable(record_fn):
        record_fn(record)
        return
    if callable(tracer):
        tracer(record)


def classify_error(exc: BaseException) -> str:
    """Map exceptions into bounded machine-readable MAS error categories."""
    if isinstance(exc, MasValidationError):
        return exc.error_category
    exc_name = type(exc).__name__.lower()
    if "interrupt" in exc_name or "cancel" in exc_name:
        return "cancelled"
    if isinstance(exc, ProviderAuthError):
        return "provider_auth"
    if isinstance(exc, ProviderRateLimitError):
        return "provider_rate_limit"
    if isinstance(exc, (ProviderTimeoutError, TimeoutError)):
        return "provider_timeout"
    if isinstance(exc, ProviderResponseError):
        return "provider_response_error"
    if isinstance(exc, ProviderError):
        return "provider_unavailable"
    if isinstance(exc, ValueError):
        msg = str(exc).lower()
        if "json" in msg:
            return "invalid_json"
        if "schema" in msg:
            return "schema_violation"
        if "no " in msg and "in the input" in msg:
            return "unsupported_observation"
        return "validation_error"
    return "execution_error"


def safe_error_summary(exc: BaseException, *, secrets: Iterable[str] = ()) -> str:
    """Return a bounded, secret-redacted error summary without payload content."""
    category = classify_error(exc)
    if isinstance(exc, MasValidationError):
        return redact_reason(str(exc), secrets) or category
    if isinstance(exc, (ProviderError, ValueError)):
        return redact_reason(str(exc), secrets) or category
    return category


def _absent_sources(
    context_or_payload: Mapping[str, Any],
    coverage: Mapping[str, SourceCoverage] | None = None,
) -> set[str]:
    absent: set[str] = set()
    for source in ("events", "logs", "metrics", "checks"):
        if source in context_or_payload and not context_or_payload.get(source):
            absent.add(source)
        elif coverage is not None and source in coverage:
            cov = coverage[source]
            if cov.count == 0 or cov.status in _DEGRADED_OR_EMPTY_STATUSES:
                absent.add(source)
    return absent


def detect_affirmative_source_claims(
    text: str,
    absent_sources: Iterable[str],
    *,
    field: str = "text",
) -> list[EvidenceClaimIssue]:
    """Detect affirmative claims about absent/uncollected evidence sources."""
    if not text or not text.strip():
        return []
    sources = tuple(s for s in absent_sources if s in _AFFIRMATIVE_SOURCE_PATTERNS)
    if not sources:
        return []

    issues: list[EvidenceClaimIssue] = []
    for raw_clause in _CLAUSE_SPLIT_RE.split(text):
        clause = raw_clause.strip()
        if not clause:
            continue
        if _ABSENCE_OR_HEDGE_RE.search(clause):
            continue
        for source in sources:
            pattern = _AFFIRMATIVE_SOURCE_PATTERNS[source]
            if pattern.search(clause):
                issues.append(
                    EvidenceClaimIssue(
                        source=source,
                        field=field,
                        category="unsupported_observation",
                        detail=f"Affirmative {source} claim in {field} while {source} is absent.",
                    )
                )
    return issues


def _collect_refs_from_text(text: str, refs: set[str]) -> None:
    for match in _K8S_REF_RE.finditer(text):
        kind = _KIND_ALIASES.get(match.group("kind"), match.group("kind"))
        refs.add(f"{kind}/{match.group('name')}")


def _extract_pod_containers(spec: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    containers: list[Mapping[str, Any]] = []
    for key in ("containers", "initContainers", "ephemeralContainers"):
        raw = spec.get(key)
        if isinstance(raw, list):
            containers.extend(c for c in raw if isinstance(c, Mapping))
    template = spec.get("template")
    if isinstance(template, Mapping) and isinstance(template.get("spec"), Mapping):
        containers.extend(_extract_pod_containers(template["spec"]))
    job_template = spec.get("jobTemplate")
    if isinstance(job_template, Mapping) and isinstance(job_template.get("spec"), Mapping):
        containers.extend(_extract_pod_containers(job_template["spec"]))
    return containers


def extract_known_references(context: Mapping[str, Any]) -> KnownContextReferences:
    """Extract stable Kubernetes and telemetry identifiers from IncidentContext."""
    resource_refs: set[str] = set()
    controller_names: set[str] = set()
    pod_names: set[str] = set()
    container_names: set[str] = set()
    log_sources: set[str] = set()
    metric_names: set[str] = set()
    check_targets: set[str] = set()

    incident = context.get("incident")
    if isinstance(incident, str):
        _collect_refs_from_text(incident, resource_refs)

    resources = context.get("resources")
    has_resources = isinstance(resources, list) and len(resources) > 0
    if isinstance(resources, list):
        for res in resources:
            if not isinstance(res, Mapping):
                continue
            kind = res.get("kind")
            meta = res.get("metadata")
            name = meta.get("name") if isinstance(meta, Mapping) else None
            if isinstance(kind, str) and kind and isinstance(name, str) and name:
                canonical_kind = _KIND_ALIASES.get(kind, kind)
                resource_refs.add(f"{canonical_kind}/{name}")
                if canonical_kind in _POD_CONTROLLER_KINDS:
                    controller_names.add(name)
                if canonical_kind == "Pod":
                    pod_names.add(name)
            spec = res.get("spec")
            if not isinstance(spec, Mapping):
                continue
            if isinstance(spec.get("storageClassName"), str) and spec["storageClassName"]:
                resource_refs.add(f"StorageClass/{spec['storageClassName']}")
            for sa_key in ("serviceAccountName", "serviceAccount"):
                sa = spec.get(sa_key)
                if isinstance(sa, str) and sa:
                    resource_refs.add(f"ServiceAccount/{sa}")
            template = spec.get("template")
            if isinstance(template, Mapping) and isinstance(template.get("spec"), Mapping):
                tpl_spec = template["spec"]
                for sa_key in ("serviceAccountName", "serviceAccount"):
                    sa = tpl_spec.get(sa_key)
                    if isinstance(sa, str) and sa:
                        resource_refs.add(f"ServiceAccount/{sa}")
                _extract_volume_refs(tpl_spec.get("volumes"), resource_refs)
            _extract_volume_refs(spec.get("volumes"), resource_refs)
            role_ref = res.get("roleRef")
            if isinstance(role_ref, Mapping):
                rk, rn = role_ref.get("kind"), role_ref.get("name")
                if isinstance(rk, str) and rk and isinstance(rn, str) and rn:
                    resource_refs.add(f"{rk}/{rn}")
            for container in _extract_pod_containers(spec):
                cname = container.get("name")
                if isinstance(cname, str) and cname:
                    container_names.add(cname)
                env_list = container.get("env")
                if isinstance(env_list, list):
                    for env_entry in env_list:
                        if not isinstance(env_entry, Mapping):
                            continue
                        val_from = env_entry.get("valueFrom")
                        if not isinstance(val_from, Mapping):
                            continue
                        sec_ref = val_from.get("secretKeyRef")
                        if isinstance(sec_ref, Mapping) and isinstance(sec_ref.get("name"), str):
                            resource_refs.add(f"Secret/{sec_ref['name']}")
                        cm_ref = val_from.get("configMapKeyRef")
                        if isinstance(cm_ref, Mapping) and isinstance(cm_ref.get("name"), str):
                            resource_refs.add(f"ConfigMap/{cm_ref['name']}")

    events = context.get("events")
    if isinstance(events, list):
        for ev in events:
            if not isinstance(ev, Mapping):
                continue
            inv = ev.get("involved_object") or ev.get("involvedObject")
            if isinstance(inv, str) and "/" in inv:
                _collect_refs_from_text(inv, resource_refs)
                if inv.startswith("Pod/"):
                    pod_names.add(inv.split("/", 1)[1])
            elif isinstance(inv, Mapping):
                ek, en = inv.get("kind"), inv.get("name")
                if isinstance(ek, str) and ek and isinstance(en, str) and en:
                    resource_refs.add(f"{_KIND_ALIASES.get(ek, ek)}/{en}")
                    if ek == "Pod":
                        pod_names.add(en)

    logs = context.get("logs")
    if isinstance(logs, list):
        for entry in logs:
            if not isinstance(entry, Mapping):
                continue
            for key in ("container", "pod", "source", "name"):
                val = entry.get(key)
                if isinstance(val, str) and val:
                    log_sources.add(val)
                    if key == "pod":
                        pod_names.add(val)
                        resource_refs.add(f"Pod/{val}")

    metrics = context.get("metrics")
    if isinstance(metrics, list):
        for entry in metrics:
            if not isinstance(entry, Mapping):
                continue
            for key in ("metric_name", "query", "name"):
                val = entry.get(key)
                if isinstance(val, str) and val:
                    metric_names.add(val)
            metric_field = entry.get("metric")
            if isinstance(metric_field, str) and metric_field:
                metric_names.add(metric_field)
            elif isinstance(metric_field, Mapping):
                mname = metric_field.get("__name__")
                if isinstance(mname, str) and mname:
                    metric_names.add(mname)
                mpod = metric_field.get("pod")
                if isinstance(mpod, str) and mpod:
                    pod_names.add(mpod)
            mpod = entry.get("pod")
            if isinstance(mpod, str) and mpod:
                pod_names.add(mpod)
            mnode = entry.get("node")
            if isinstance(mnode, str) and mnode:
                resource_refs.add(f"Node/{mnode}")

    checks = context.get("checks")
    if isinstance(checks, list):
        for chk in checks:
            if not isinstance(chk, Mapping):
                continue
            for key in ("target", "check", "source"):
                val = chk.get(key)
                if isinstance(val, str) and val:
                    check_targets.add(val)

    return KnownContextReferences(
        has_resources=has_resources,
        resource_refs=frozenset(resource_refs),
        controller_names=frozenset(controller_names),
        pod_names=frozenset(pod_names),
        container_names=frozenset(container_names),
        log_sources=frozenset(log_sources),
        metric_names=frozenset(metric_names),
        check_targets=frozenset(check_targets),
    )


def _extract_volume_refs(volumes: Any, refs: set[str]) -> None:
    if not isinstance(volumes, list):
        return
    for vol in volumes:
        if not isinstance(vol, Mapping):
            continue
        cm = vol.get("configMap")
        if isinstance(cm, Mapping) and isinstance(cm.get("name"), str) and cm["name"]:
            refs.add(f"ConfigMap/{cm['name']}")
        sec = vol.get("secret")
        if isinstance(sec, Mapping) and isinstance(sec.get("secretName"), str) and sec["secretName"]:
            refs.add(f"Secret/{sec['secretName']}")
        pvc = vol.get("persistentVolumeClaim")
        if isinstance(pvc, Mapping) and isinstance(pvc.get("claimName"), str) and pvc["claimName"]:
            refs.add(f"PersistentVolumeClaim/{pvc['claimName']}")


def _check_text_k8s_refs(
    text: str,
    known_refs: KnownContextReferences,
    *,
    field: str,
) -> list[EvidenceClaimIssue]:
    if not text or not known_refs.has_resources:
        return []
    issues: list[EvidenceClaimIssue] = []
    for match in _K8S_REF_RE.finditer(text):
        kind = match.group("kind")
        name = match.group("name")
        if not known_refs.knows_ref(kind, name):
            issues.append(
                EvidenceClaimIssue(
                    source="resources",
                    field=field,
                    category="unverified_reference",
                    detail=f"Referenced {kind}/{name} in {field} is not present in context.",
                )
            )
    return issues


def validate_specialist_claims(
    specialist: str,
    report: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    context: Mapping[str, Any] | None = None,
    coverage: Mapping[str, SourceCoverage] | None = None,
) -> list[EvidenceClaimIssue]:
    """Run deterministic claim and reference checks on a validated specialist report."""
    issues: list[EvidenceClaimIssue] = []
    absent = _absent_sources(payload, coverage)
    if coverage is not None and "resources" in coverage:
        res_cov = coverage["resources"]
        if res_cov.count == 0 or res_cov.status in _DEGRADED_OR_EMPTY_STATUSES:
            absent.add("resources")

    # 1. Affirmative prose claims for absent sources.
    if isinstance(report.get("preliminary_assessment"), str):
        issues.extend(
            detect_affirmative_source_claims(
                report["preliminary_assessment"],
                absent,
                field="preliminary_assessment",
            )
        )
    if specialist == "logs-connectivity" and isinstance(report.get("runtime_failure_mode"), str):
        issues.extend(
            detect_affirmative_source_claims(
                report["runtime_failure_mode"],
                absent,
                field="runtime_failure_mode",
            )
        )

    # 2. Stable reference verification when source objects exist.
    ref_context = context if context is not None else payload
    known_refs = extract_known_references(ref_context)
    if known_refs.has_resources:
        if specialist == "k8s-config-events":
            for idx, anomaly in enumerate(report.get("anomalies", [])):
                res = anomaly.get("resource")
                if isinstance(res, str):
                    issues.extend(
                        _check_text_k8s_refs(res, known_refs, field=f"anomalies[{idx}].resource")
                    )
            for idx, res in enumerate(report.get("suspicious_resources", [])):
                if isinstance(res, str):
                    issues.extend(
                        _check_text_k8s_refs(
                            res, known_refs, field=f"suspicious_resources[{idx}]"
                        )
                    )
            for idx, ev in enumerate(report.get("significant_events", [])):
                obj = ev.get("involved_object")
                if isinstance(obj, str):
                    issues.extend(
                        _check_text_k8s_refs(
                            obj, known_refs, field=f"significant_events[{idx}].involved_object"
                        )
                    )
        if isinstance(report.get("preliminary_assessment"), str):
            issues.extend(
                _check_text_k8s_refs(
                    report["preliminary_assessment"],
                    known_refs,
                    field="preliminary_assessment",
                )
            )

    if specialist == "logs-connectivity" and payload.get("logs") and known_refs.log_sources:
        allowed_log_sources = {s.casefold() for s in known_refs.log_sources}
        for idx, item in enumerate(report.get("log_findings", [])):
            src = str(item.get("source", "")).strip()
            if src and src.casefold() not in allowed_log_sources:
                issues.append(
                    EvidenceClaimIssue(
                        source="logs",
                        field=f"log_findings[{idx}].source",
                        category="unverified_reference",
                        detail=f"Log source {src!r} does not match any container or pod in logs.",
                    )
                )

    if specialist == "metrics-telemetry" and payload.get("metrics") and known_refs.metric_names:
        allowed_metrics = {m.casefold() for m in known_refs.metric_names}
        for idx, item in enumerate(report.get("resource_findings", [])):
            if item.get("status") == "uncollected":
                continue
            mname = str(item.get("metric_name", "")).strip()
            if mname and mname.casefold() not in allowed_metrics:
                issues.append(
                    EvidenceClaimIssue(
                        source="metrics",
                        field=f"resource_findings[{idx}].metric_name",
                        category="unverified_reference",
                        detail=f"Metric {mname!r} is not present in collected metrics.",
                    )
                )

    return issues


def validate_specialist_report(
    specialist: str,
    raw: str,
    payload: Mapping[str, Any],
    *,
    context: Mapping[str, Any] | None = None,
    coverage: Mapping[str, SourceCoverage] | None = None,
    strict: bool = True,
) -> tuple[dict[str, Any], list[EvidenceClaimIssue]]:
    """Parse, schema-validate, and evidence-check a single MAS specialist response."""
    schema = REPORT_SCHEMAS[specialist]
    label = f"MAS {specialist} report"
    try:
        report = parse_and_validate_json(raw, schema, label=label)
    except ValueError as error:
        raise MasValidationError(
            str(error),
            agent=specialist,
            error_category=classify_error(error),
        ) from None

    try:
        validate_observations(report, payload)
    except ValueError as error:
        issue = EvidenceClaimIssue(
            source="observations",
            field="root",
            category="unsupported_observation",
            detail=str(error),
        )
        raise MasValidationError(
            str(error),
            agent=specialist,
            error_category="unsupported_observation",
            issues=(issue,),
        ) from None

    issues = validate_specialist_claims(
        specialist,
        report,
        payload,
        context=context,
        coverage=coverage,
    )
    if strict and issues:
        first = issues[0]
        raise MasValidationError(
            f"MAS {specialist} failed {first.category} check: {first.detail}",
            agent=specialist,
            error_category=first.category,
            issues=issues,
        )
    return report, issues


def validate_coordinator_rca(
    raw: str,
    context: Mapping[str, Any],
    agent_reports: Mapping[str, Mapping[str, Any]],
    *,
    coverage: Mapping[str, SourceCoverage] | None = None,
    strict: bool = True,
) -> tuple[dict[str, Any], list[EvidenceClaimIssue]]:
    """Parse, schema-validate, and evidence-check the MAS coordinator RCA output."""
    try:
        parsed = parse_and_validate_json(raw, RCA, label="MAS coordinator RCA")
    except ValueError as error:
        raise MasValidationError(
            str(error),
            agent="coordinator",
            error_category=classify_error(error),
        ) from None

    issues: list[EvidenceClaimIssue] = []
    absent = _absent_sources(context, coverage)
    known_refs = extract_known_references(context)

    for field_name in ("cause", "causal_chain"):
        val = parsed.get(field_name)
        if isinstance(val, str):
            issues.extend(detect_affirmative_source_claims(val, absent, field=field_name))
            issues.extend(_check_text_k8s_refs(val, known_refs, field=field_name))

    for idx, item in enumerate(parsed.get("supporting_evidence", [])):
        if isinstance(item, str):
            issues.extend(
                detect_affirmative_source_claims(
                    item, absent, field=f"supporting_evidence[{idx}]"
                )
            )
            issues.extend(
                _check_text_k8s_refs(item, known_refs, field=f"supporting_evidence[{idx}]")
            )

    # Check contradiction with metrics specialist when metrics were absent.
    metrics_report = agent_reports.get("metrics-telemetry", {})
    if (
        "metrics" in absent
        and not metrics_report.get("throttling_detected")
        and metrics_report.get("saturation_identified") is None
    ):
        for field_name in ("cause", "causal_chain"):
            val = parsed.get(field_name, "")
            if isinstance(val, str) and not _ABSENCE_OR_HEDGE_RE.search(val):
                if re.search(
                    r"\bmetrics?\s+(?:confirm|show|prove|measured)\b",
                    val,
                    re.IGNORECASE,
                ):
                    issues.append(
                        EvidenceClaimIssue(
                            source="metrics",
                            field=field_name,
                            category="contradictory_finding",
                            detail="Coordinator claims metrics confirmation despite uncollected metrics.",
                        )
                    )

    if strict and issues:
        first = issues[0]
        raise MasValidationError(
            f"MAS coordinator failed {first.category} check: {first.detail}",
            agent="coordinator",
            error_category=first.category,
            issues=issues,
        )
    return parsed, issues


def build_validated_mas_workflow(
    *,
    complete: CompleteFn,
    max_concurrency: int | None = None,
    coverage: Mapping[str, SourceCoverage] | None = None,
    tracer: MasTracer | None = None,
    strict_claims: bool = True,
    secrets: Iterable[str] = (),
) -> Any:
    """Build a compiled 4-call MAS LangGraph with evidence validation and per-agent tracing."""
    from rca.workflows.mas import (
        MasWorkflowState,
        _build_specialist_payload,
        _resolve_max_concurrency,
    )

    if not callable(complete):
        raise TypeError("complete must be a callable accepting (prompt_path, payload) -> str.")

    effective_concurrency = _resolve_max_concurrency(complete, max_concurrency)
    secret_tuple = tuple(s for s in secrets if s)
    trace_lock = threading.Lock()
    coordinator_blocked = False

    def _emit_coordinator_blocked(failing_agent: str, category: str) -> None:
        nonlocal coordinator_blocked
        with trace_lock:
            if coordinator_blocked:
                return
            coordinator_blocked = True
        _emit_trace(
            tracer,
            AgentExecutionTrace(
                agent="coordinator",
                status="blocked",
                duration_seconds=0.0,
                error_category="blocked_by_specialist",
                error_message=f"Coordinator blocked after {failing_agent} ({category}).",
            ),
        )

    def _make_specialist_node(specialist: str):
        prompt_path = SPECIALIST_PROMPTS[specialist]
        state_key = SPECIALIST_STATE_KEYS[specialist]

        def _run_specialist(state: MasWorkflowState) -> MasWorkflowState:
            started = time.perf_counter()
            ctx = state["context"]
            payload = _build_specialist_payload(specialist, ctx)
            try:
                raw = complete(prompt_path, payload)
                report, _ = validate_specialist_report(
                    specialist,
                    raw,
                    payload,
                    context=ctx,
                    coverage=coverage,
                    strict=strict_claims,
                )
            except BaseException as exc:
                duration = round(time.perf_counter() - started, 4)
                category = classify_error(exc)
                status: Literal["failed", "cancelled"] = (
                    "cancelled" if category == "cancelled" else "failed"
                )
                _emit_trace(
                    tracer,
                    AgentExecutionTrace(
                        agent=specialist,
                        status=status,
                        duration_seconds=duration,
                        error_category=category,
                        error_message=safe_error_summary(exc, secrets=secret_tuple),
                    ),
                )
                _emit_coordinator_blocked(specialist, category)
                raise

            _emit_trace(
                tracer,
                AgentExecutionTrace(
                    agent=specialist,
                    status="passed",
                    duration_seconds=round(time.perf_counter() - started, 4),
                ),
            )
            return {state_key: report}  # type: ignore[misc]

        _run_specialist.__name__ = f"specialist_{specialist.replace('-', '_')}"
        return _run_specialist

    def coordinator(state: MasWorkflowState) -> MasWorkflowState:
        started = time.perf_counter()
        ctx = state["context"]
        agent_reports = {
            name: state[SPECIALIST_STATE_KEYS[name]]  # type: ignore[literal-required]
            for name in SPECIALIST_ORDER
        }
        try:
            raw = complete(
                COORDINATOR_PROMPT_PATH,
                {"context": ctx, "agent_reports": agent_reports},
            )
            validated, _ = validate_coordinator_rca(
                raw,
                ctx,
                agent_reports,
                coverage=coverage,
                strict=strict_claims,
            )
        except BaseException as exc:
            duration = round(time.perf_counter() - started, 4)
            category = classify_error(exc)
            status: Literal["failed", "cancelled"] = (
                "cancelled" if category == "cancelled" else "failed"
            )
            _emit_trace(
                tracer,
                AgentExecutionTrace(
                    agent="coordinator",
                    status=status,
                    duration_seconds=duration,
                    error_category=category,
                    error_message=safe_error_summary(exc, secrets=secret_tuple),
                ),
            )
            raise

        _emit_trace(
            tracer,
            AgentExecutionTrace(
                agent="coordinator",
                status="passed",
                duration_seconds=round(time.perf_counter() - started, 4),
            ),
        )
        return {"agent_reports": agent_reports, "root-cause": validated}

    graph = StateGraph(MasWorkflowState)
    for specialist in SPECIALIST_ORDER:
        graph.add_node(specialist, _make_specialist_node(specialist))
        graph.add_edge(START, specialist)

    graph.add_node("coordinator", coordinator)
    graph.add_edge(list(SPECIALIST_ORDER), "coordinator")
    graph.add_edge("coordinator", END)

    return graph.compile().with_config({"max_concurrency": effective_concurrency})
