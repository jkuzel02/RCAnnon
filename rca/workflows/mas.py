"""Four-call parallel multi-agent (MAS) LangGraph RCA workflow."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from rca.contracts.provider import CompleteFn
from rca.contracts.schemas import RCA, REPORT_SCHEMAS, parse_and_validate_json

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

MasWorkflowState = TypedDict(
    "MasWorkflowState",
    {
        "context": dict[str, Any],
        "k8s_config_events_report": dict[str, Any],
        "logs_connectivity_report": dict[str, Any],
        "metrics_telemetry_report": dict[str, Any],
        "agent_reports": dict[str, dict[str, Any]],
        "root-cause": dict[str, Any],
    },
    total=False,
)


def _extract_pod_spec(spec: Mapping[str, Any]) -> Mapping[str, Any]:
    job_template = spec.get("jobTemplate")
    if isinstance(job_template, Mapping):
        job_spec = job_template.get("spec")
        if isinstance(job_spec, Mapping):
            template = job_spec.get("template")
            if isinstance(template, Mapping) and isinstance(template.get("spec"), Mapping):
                return template["spec"]
    template = spec.get("template")
    if isinstance(template, Mapping) and isinstance(template.get("spec"), Mapping):
        return template["spec"]
    return spec


def extract_resource_limits(resources: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Extract normalized Kind/name container requests and limits for metrics-telemetry."""
    records: list[dict[str, Any]] = []
    for resource in resources:
        if not isinstance(resource, Mapping):
            continue
        spec = resource.get("spec")
        if not isinstance(spec, Mapping):
            continue
        pod_spec = _extract_pod_spec(spec)
        metadata = resource.get("metadata")
        if not isinstance(metadata, Mapping):
            metadata = {}
        containers = pod_spec.get("containers")
        if not isinstance(containers, list):
            continue
        for container in containers:
            if not isinstance(container, Mapping):
                continue
            settings = container.get("resources")
            if isinstance(settings, Mapping) and settings:
                requests = settings.get("requests")
                limits = settings.get("limits")
                records.append(
                    {
                        "resource": f"{resource['kind']}/{metadata['name']}",
                        "namespace": metadata.get("namespace"),
                        "container": container["name"],
                        "requests": dict(requests) if isinstance(requests, Mapping) else {},
                        "limits": dict(limits) if isinstance(limits, Mapping) else {},
                    }
                )
    return records


def _build_specialist_payload(specialist: str, context: Mapping[str, Any]) -> dict[str, Any]:
    if specialist == "k8s-config-events":
        return {
            "resources": context["resources"],
            "events": context["events"],
        }
    if specialist == "logs-connectivity":
        return {
            "logs": context["logs"],
            "checks": context["checks"],
        }
    if specialist == "metrics-telemetry":
        return {
            "metrics": context["metrics"],
            "time_window": context["time_window"],
            "resource_limits": extract_resource_limits(context["resources"]),
        }
    raise ValueError(f"Unknown MAS specialist: {specialist!r}.")


def _is_plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _resolve_max_concurrency(complete: CompleteFn, max_concurrency: int | None) -> int:
    if max_concurrency is not None:
        if not _is_plain_int(max_concurrency) or max_concurrency < 1:
            raise ValueError("max_concurrency must be a positive integer.")

    bound_self = getattr(complete, "__self__", None)
    cfg = getattr(bound_self, "config", None) if bound_self is not None else None
    cfg_cap = getattr(cfg, "max_concurrency", None) if cfg is not None else None
    if _is_plain_int(cfg_cap):
        provider_cap: int | None = cfg_cap
    else:
        direct_cap = getattr(complete, "max_concurrency", None)
        provider_cap = direct_cap if _is_plain_int(direct_cap) else None

    limit = MAX_SPECIALIST_CONCURRENCY
    if max_concurrency is not None:
        limit = min(limit, max_concurrency)
    if provider_cap is not None:
        if provider_cap < 1:
            raise ValueError("Provider max_concurrency must be a positive integer.")
        limit = min(limit, provider_cap)
    return limit


def build_mas_workflow(
    *,
    complete: CompleteFn,
    max_concurrency: int | None = None,
) -> Any:
    """Build a compiled 4-call parallel MAS LangGraph with strict report and RCA validation."""
    if not callable(complete):
        raise TypeError("complete must be a callable accepting (prompt_path, payload) -> str.")

    effective_concurrency = _resolve_max_concurrency(complete, max_concurrency)

    def _make_specialist_node(specialist: str):
        prompt_path = SPECIALIST_PROMPTS[specialist]
        state_key = SPECIALIST_STATE_KEYS[specialist]
        schema = REPORT_SCHEMAS[specialist]
        label = f"MAS {specialist} report"

        def _run_specialist(state: MasWorkflowState) -> MasWorkflowState:
            payload = _build_specialist_payload(specialist, state["context"])
            raw = complete(prompt_path, payload)
            report = parse_and_validate_json(raw, schema, label=label)
            return {state_key: report}  # type: ignore[misc]

        _run_specialist.__name__ = f"specialist_{specialist.replace('-', '_')}"
        return _run_specialist

    def coordinator(state: MasWorkflowState) -> MasWorkflowState:
        agent_reports = {
            name: state[SPECIALIST_STATE_KEYS[name]]  # type: ignore[literal-required]
            for name in SPECIALIST_ORDER
        }
        raw = complete(
            COORDINATOR_PROMPT_PATH,
            {"context": state["context"], "agent_reports": agent_reports},
        )
        validated = parse_and_validate_json(raw, RCA, label="MAS coordinator RCA")
        return {"agent_reports": agent_reports, "root-cause": validated}

    graph = StateGraph(MasWorkflowState)
    for specialist in SPECIALIST_ORDER:
        graph.add_node(specialist, _make_specialist_node(specialist))
        graph.add_edge(START, specialist)

    graph.add_node("coordinator", coordinator)
    graph.add_edge(list(SPECIALIST_ORDER), "coordinator")
    graph.add_edge("coordinator", END)

    return graph.compile().with_config({"max_concurrency": effective_concurrency})
