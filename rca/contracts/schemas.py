"""Canonical JSON schemas and prompt allowlist for RCAnnon."""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

TEXT = {"type": "string", "pattern": r"\S"}


def array(items: dict, *, minimum: int = 0) -> dict:
    return {"type": "array", "items": items, "minItems": minimum}


def object_with(**fields: dict) -> dict:
    return {
        "type": "object",
        "properties": fields,
        "required": list(fields),
        "additionalProperties": False,
    }


RCA = object_with(
    cause=TEXT,
    causal_chain=TEXT,
    supporting_evidence=array(TEXT, minimum=1),
    remediation=TEXT,
    recovery_check=TEXT,
)

CASE = object_with(
    context=object_with(
        incident=TEXT,
        time_window={"type": ["null", "string", "object"]},
        resources=array({"type": "object"}),
        events=array({}),
        logs=array({}),
        metrics=array({}),
        checks=array({}),
    ),
    **{"root-cause": RCA},
)

REPORT_SCHEMAS = {
    "k8s-config-events": object_with(
        agent={"const": "k8s-config-events"},
        anomalies=array(
            object_with(
                resource=TEXT,
                field=TEXT,
                issue=TEXT,
                severity={"enum": ["low", "medium", "high"]},
            )
        ),
        suspicious_resources=array(TEXT),
        significant_events=array(object_with(reason=TEXT, involved_object=TEXT, message=TEXT)),
        preliminary_assessment=TEXT,
    ),
    "logs-connectivity": object_with(
        agent={"const": "logs-connectivity"},
        log_findings=array(
            object_with(
                source=TEXT,
                level={"enum": ["info", "warning", "error"]},
                summary=TEXT,
            )
        ),
        connectivity_findings=array(
            object_with(target=TEXT, status={"enum": ["passed", "failed"]}, details=TEXT)
        ),
        runtime_failure_mode=TEXT,
        preliminary_assessment=TEXT,
    ),
    "metrics-telemetry": object_with(
        agent={"const": "metrics-telemetry"},
        resource_findings=array(
            object_with(
                metric_name=TEXT,
                observed_value=TEXT,
                limit_value={"anyOf": [TEXT, {"type": "null"}]},
                status={"enum": ["normal", "elevated", "saturated", "throttled", "uncollected"]},
                analysis=TEXT,
            )
        ),
        throttling_detected={"type": "boolean"},
        saturation_identified={"anyOf": [TEXT, {"type": "null"}]},
        preliminary_assessment=TEXT,
    ),
}

PROMPT_ALLOWLIST: frozenset[str] = frozenset(
    {
        "base.md",
        "optimal.md",
        "mas/k8s-config-events.md",
        "mas/logs-connectivity.md",
        "mas/metrics-telemetry.md",
        "mas/coordinator.md",
    }
)

OBSERVATION_FIELDS: dict[str, str] = {
    "events": "significant_events",
    "logs": "log_findings",
    "checks": "connectivity_findings",
}


def validate_observations(report: Mapping[str, Any], payload: Mapping[str, Any]) -> None:
    """Reject affirmative specialist findings when the corresponding input source is empty."""
    for source, findings in OBSERVATION_FIELDS.items():
        if findings in report and not payload.get(source) and report[findings]:
            raise ValueError(f"Reported {findings} with no {source} in the input.")
    if "metrics" in payload and not payload["metrics"]:
        if (
            report.get("throttling_detected")
            or report.get("saturation_identified") is not None
            or any(
                finding.get("status") != "uncollected"
                for finding in report.get("resource_findings", [])
            )
        ):
            raise ValueError("Reported measured behavior with no metrics in the input.")


def parse_and_validate_json(
    raw: str,
    schema: dict[str, Any],
    *,
    label: str = "response",
) -> dict[str, Any]:
    """Strictly parse a JSON object string and validate it against a JSON Schema."""
    if not isinstance(raw, str):
        raise ValueError(f"Invalid {label}: expected a JSON string.")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"Invalid {label} JSON (line {error.lineno}, column {error.colno})."
        ) from None
    try:
        Draft202012Validator(schema).validate(parsed)
    except ValidationError as error:
        location = ".".join(map(str, error.absolute_path)) or "root"
        raise ValueError(
            f"Invalid {label} schema at {location} ({error.validator})."
        ) from None
    return parsed
