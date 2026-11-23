"""Contract tests for the ratified v1 architecture and golden fixtures."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from unittest.mock import Mock

import pytest
from jsonschema import Draft202012Validator

from rca.contracts.provider import ProviderTimeoutError
from rca.contracts.schemas import CASE, PROMPT_ALLOWLIST, RCA
from rca.workflows import build_workflow, list_variants

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "contracts"
ARCHITECTURE_DOC = REPO_ROOT / "docs" / "architecture.md"

COVERAGE_STATUSES = [
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
DEGRADED_STATUSES = {
    "unsupported",
    "forbidden",
    "unreachable",
    "timeout",
    "truncated",
    "invalid",
}
CANONICAL_SOURCES = ("resources", "events", "logs", "metrics", "checks")
ISO_UTC_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$"
HEX_SHA256_PATTERN = r"^[0-9a-f]{64}$"

SOURCE_COVERAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"enum": COVERAGE_STATUSES},
        "count": {"type": "integer", "minimum": 0},
        "reason": {"type": ["string", "null"]},
        "truncated": {"type": "boolean"},
    },
    "required": ["status", "count", "reason", "truncated"],
    "additionalProperties": False,
}

EVIDENCE_SNAPSHOT_V1_SCHEMA = {
    "type": "object",
    "properties": {
        "schema_version": {"const": "v1"},
        "snapshot_id": {"type": "string", "minLength": 1},
        "scope_hash": {"type": "string", "pattern": HEX_SHA256_PATTERN},
        "source_type": {"enum": ["live", "cached", "bundle", "fixture"]},
        "context_alias": {"type": ["string", "null"]},
        "namespaces": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "collected_at": {"type": "string", "pattern": ISO_UTC_PATTERN},
        "observed_at": {
            "anyOf": [
                {"type": "string", "pattern": ISO_UTC_PATTERN},
                {"type": "null"},
            ]
        },
        "context": CASE["properties"]["context"],
        "coverage": {
            "type": "object",
            "properties": {source: SOURCE_COVERAGE_SCHEMA for source in CANONICAL_SOURCES},
            "required": list(CANONICAL_SOURCES),
            "additionalProperties": False,
        },
    },
    "required": [
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
    ],
    "additionalProperties": False,
}

RUN_USAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "duration_seconds": {"type": ["number", "null"], "minimum": 0},
        "logical_calls": {"type": "integer", "minimum": 0},
        "reported_input_tokens": {"type": ["integer", "null"], "minimum": 0},
        "reported_output_tokens": {"type": ["integer", "null"], "minimum": 0},
        "reported_total_tokens": {"type": ["integer", "null"], "minimum": 0},
        "unknown_token_calls": {"type": "integer", "minimum": 0},
    },
    "required": [
        "duration_seconds",
        "logical_calls",
        "reported_input_tokens",
        "reported_output_tokens",
        "reported_total_tokens",
        "unknown_token_calls",
    ],
    "additionalProperties": False,
}

RUN_RESULT_V1_SCHEMA = {
    "type": "object",
    "properties": {
        "schema_version": {"const": "v1"},
        "request_id": {"type": "string", "minLength": 1},
        "run_id": {"type": "string", "minLength": 1},
        "variant": {"enum": ["base", "optimal", "mas"]},
        "snapshot_id": {"type": ["string", "null"]},
        "snapshot_hash": {
            "anyOf": [
                {"type": "string", "pattern": HEX_SHA256_PATTERN},
                {"type": "null"},
            ]
        },
        "status": {
            "enum": [
                "running",
                "diagnosed",
                "qualified",
                "insufficient_evidence",
                "failed",
                "interrupted",
            ]
        },
        "coverage": {
            "type": "object",
            "properties": {source: SOURCE_COVERAGE_SCHEMA for source in CANONICAL_SOURCES},
            "required": list(CANONICAL_SOURCES),
            "additionalProperties": False,
        },
        "limitations": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "result": {"anyOf": [{"type": "string", "minLength": 1}, RCA, {"type": "null"}]},
        "error_code": {"type": ["string", "null"]},
        "error_message": {"type": ["string", "null"]},
        "usage": RUN_USAGE_SCHEMA,
        "created_at": {"type": "string", "pattern": ISO_UTC_PATTERN},
        "completed_at": {
            "anyOf": [
                {"type": "string", "pattern": ISO_UTC_PATTERN},
                {"type": "null"},
            ]
        },
    },
    "required": [
        "schema_version",
        "request_id",
        "run_id",
        "variant",
        "snapshot_id",
        "snapshot_hash",
        "status",
        "coverage",
        "limitations",
        "result",
        "error_code",
        "error_message",
        "usage",
        "created_at",
        "completed_at",
    ],
    "additionalProperties": False,
}

SNAPSHOT_VALIDATOR = Draft202012Validator(EVIDENCE_SNAPSHOT_V1_SCHEMA)
RUN_RESULT_VALIDATOR = Draft202012Validator(RUN_RESULT_V1_SCHEMA)
RCA_VALIDATOR = Draft202012Validator(RCA)

SPECIALIST_REPORTS = {
    "k8s-config-events": {
        "agent": "k8s-config-events",
        "anomalies": [
            {
                "resource": "Deployment/checkout",
                "field": "spec.template.spec.containers[0].resources.limits.memory",
                "issue": "Memory limit 64Mi is tight for observed working set.",
                "severity": "high",
            }
        ],
        "suspicious_resources": ["Deployment/checkout", "Pod/checkout-7d9b4f8c9-x2k9p"],
        "significant_events": [
            {
                "reason": "OOMKilled",
                "involved_object": "Pod/checkout-7d9b4f8c9-x2k9p",
                "message": "Container web exceeded its 64Mi memory limit.",
            }
        ],
        "preliminary_assessment": "Container memory limit appears lower than runtime usage.",
    },
    "logs-connectivity": {
        "agent": "logs-connectivity",
        "log_findings": [
            {
                "source": "Pod/checkout-7d9b4f8c9-x2k9p/web",
                "level": "warning",
                "summary": "retained_mib=62 near 64Mi cgroup limit",
            }
        ],
        "connectivity_findings": [
            {
                "target": "Service/checkout",
                "status": "failed",
                "details": "502 Bad Gateway during container restart",
            }
        ],
        "runtime_failure_mode": "oom_restart",
        "preliminary_assessment": "Logs indicate memory growth prior to restart.",
    },
    "metrics-telemetry": {
        "agent": "metrics-telemetry",
        "resource_findings": [
            {
                "metric_name": "container_memory_working_set_bytes",
                "observed_value": "64Mi",
                "limit_value": "64Mi",
                "status": "saturated",
                "analysis": "Working set reached the 64Mi container memory limit.",
            }
        ],
        "throttling_detected": False,
        "saturation_identified": "memory",
        "preliminary_assessment": "Container memory saturation correlates with OOMKilled restarts.",
    },
}


def load_fixture(filename: str) -> dict:
    return json.loads((FIXTURES_DIR / filename).read_text(encoding="utf-8"))


def compute_canonical_scope_hash(
    context_alias: str | None,
    namespaces: list[str],
    context: dict,
) -> str:
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


def test_architecture_doc_exists_and_lists_all_golden_fixtures():
    content = ARCHITECTURE_DOC.read_text(encoding="utf-8")
    fixture_files = sorted(path.name for path in FIXTURES_DIR.glob("*.json"))
    assert fixture_files == [
        "run_result_base.json",
        "run_result_provider_failure.json",
        "run_result_structured.json",
        "snapshot_empty.json",
        "snapshot_full.json",
        "snapshot_partial.json",
        "ui_chat_completion_stream.json",
    ]
    for filename in fixture_files:
        assert f"tests/fixtures/contracts/{filename}" in content


@pytest.mark.parametrize(
    "filename",
    ["snapshot_full.json", "snapshot_partial.json", "snapshot_empty.json"],
)
def test_snapshot_fixtures_conform_to_v1_contract(filename: str):
    snapshot = load_fixture(filename)
    SNAPSHOT_VALIDATOR.validate(snapshot)

    expected_hash = compute_canonical_scope_hash(
        snapshot["context_alias"],
        snapshot["namespaces"],
        snapshot["context"],
    )
    assert snapshot["scope_hash"] == expected_hash
    assert "root-cause" not in snapshot
    assert "root-cause" not in snapshot["context"]

    for source in CANONICAL_SOURCES:
        assert snapshot["coverage"][source]["count"] == len(snapshot["context"][source])


def test_snapshot_full_partial_and_empty_semantics():
    full = load_fixture("snapshot_full.json")
    assert all(full["coverage"][s]["status"] == "observed" for s in CANONICAL_SOURCES)
    assert all(len(full["context"][s]) > 0 for s in CANONICAL_SOURCES)

    partial = load_fixture("snapshot_partial.json")
    assert any(len(partial["context"][s]) > 0 for s in CANONICAL_SOURCES)
    assert any(
        partial["coverage"][s]["status"] in DEGRADED_STATUSES or partial["coverage"][s]["truncated"]
        for s in CANONICAL_SOURCES
    )

    empty = load_fixture("snapshot_empty.json")
    assert all(len(empty["context"][s]) == 0 for s in CANONICAL_SOURCES)
    assert empty["observed_at"] is None


def test_run_result_base_conforms_to_v1_contract():
    full = load_fixture("snapshot_full.json")
    run = load_fixture("run_result_base.json")
    RUN_RESULT_VALIDATOR.validate(run)

    assert run["variant"] == "base"
    assert run["status"] == "diagnosed"
    assert isinstance(run["result"], str)
    assert run["limitations"] == []
    assert run["error_code"] is None
    assert run["error_message"] is None
    assert run["snapshot_id"] == full["snapshot_id"]
    assert run["snapshot_hash"] == full["scope_hash"]
    assert run["usage"]["logical_calls"] == 1
    assert run["usage"]["unknown_token_calls"] == 0


def test_run_result_structured_conforms_to_v1_contract():
    partial = load_fixture("snapshot_partial.json")
    run = load_fixture("run_result_structured.json")
    RUN_RESULT_VALIDATOR.validate(run)

    assert run["variant"] == "mas"
    assert run["status"] == "qualified"
    assert isinstance(run["result"], dict)
    RCA_VALIDATOR.validate(run["result"])
    assert "confidence" not in run["result"]
    assert len(run["limitations"]) > 0
    assert run["snapshot_id"] == partial["snapshot_id"]
    assert run["snapshot_hash"] == partial["scope_hash"]
    assert run["usage"]["logical_calls"] == 4


def test_run_result_provider_failure_conforms_to_v1_contract():
    full = load_fixture("snapshot_full.json")
    run = load_fixture("run_result_provider_failure.json")
    RUN_RESULT_VALIDATOR.validate(run)

    assert run["variant"] == "optimal"
    assert run["status"] == "failed"
    assert run["result"] is None
    assert run["error_code"] == "provider_timeout"
    assert run["error_message"]
    assert run["snapshot_id"] == full["snapshot_id"]
    assert run["snapshot_hash"] == full["scope_hash"]
    assert run["usage"]["unknown_token_calls"] == 1
    assert run["usage"]["reported_total_tokens"] is None


def test_golden_snapshots_and_outputs_work_with_build_workflow():
    assert set(list_variants()) == {"base", "optimal", "mas"}
    full_context = load_fixture("snapshot_full.json")["context"]
    base_text = load_fixture("run_result_base.json")["result"]
    structured_rca = load_fixture("run_result_structured.json")["result"]

    base_complete = Mock(return_value=base_text)
    base_out = build_workflow("base", complete=base_complete).invoke({"context": full_context})
    base_complete.assert_called_once_with("base.md", {"context": full_context})
    assert base_out["root-cause"] == base_text

    optimal_complete = Mock(return_value=json.dumps(structured_rca))
    optimal_out = build_workflow("optimal", complete=optimal_complete).invoke(
        {"context": full_context}
    )
    optimal_complete.assert_called_once_with("optimal.md", {"context": full_context})
    assert optimal_out["root-cause"] == structured_rca

    mas_responses = {f"mas/{name}.md": report for name, report in SPECIALIST_REPORTS.items()}
    mas_responses["mas/coordinator.md"] = structured_rca
    called_prompts: list[str] = []

    def mas_complete(prompt_path: str, payload: dict) -> str:
        assert prompt_path in PROMPT_ALLOWLIST
        called_prompts.append(prompt_path)
        if prompt_path == "mas/metrics-telemetry.md":
            assert payload["resource_limits"] == [
                {
                    "resource": "Deployment/checkout",
                    "namespace": "demo",
                    "container": "web",
                    "requests": {"cpu": "25m", "memory": "32Mi"},
                    "limits": {"cpu": "500m", "memory": "64Mi"},
                }
            ]
        return json.dumps(mas_responses[prompt_path])

    mas_out = build_workflow("mas", complete=mas_complete).invoke({"context": full_context})
    assert len(called_prompts) == 4
    assert called_prompts[-1] == "mas/coordinator.md"
    assert mas_out["root-cause"] == structured_rca


@pytest.mark.parametrize("variant", ["base", "optimal", "mas"])
def test_provider_timeout_propagates_for_all_variants(variant: str):
    full_context = load_fixture("snapshot_full.json")["context"]
    timeout = ProviderTimeoutError("Upstream model provider timed out after 30s.")
    graph = build_workflow(variant, complete=Mock(side_effect=timeout))

    with pytest.raises(ProviderTimeoutError) as error:
        graph.invoke({"context": full_context})

    assert error.value is timeout


def test_ui_chat_completion_stream_fixture_contract():
    fixture = load_fixture("ui_chat_completion_stream.json")
    req = fixture["request"]
    resp = fixture["response"]

    assert req["method"] == "POST"
    assert req["path"] == "/v1/chat/completions"
    auth = req["headers"]["Authorization"]
    assert auth.startswith("Bearer ")
    assert not re.search(r"\b(sk-|ghp_)", auth)

    body = req["body"]
    assert body["model"] in {"rcannon-base", "rcannon-optimal", "rcannon-mas"}
    assert body["stream"] is True
    assert "tools" not in body
    assert len(body["messages"]) == 1
    assert body["messages"][0]["role"] == "user"
    assert body["messages"][0]["content"].startswith("/rca context=lab namespace=demo -- ")

    assert resp["status"] == 200
    assert resp["headers"]["Content-Type"].startswith("text/event-stream")
    events = resp["sse_events"]
    assert events[-1] == "[DONE]"

    chunks = events[:-1]
    assert len(chunks) >= 2
    for chunk in chunks:
        assert chunk["object"] == "chat.completion.chunk"
        assert chunk["model"] == body["model"]

    rendered = chunks[0]["choices"][0]["delta"]["content"]
    assert "<" not in rendered and ">" not in rendered
    assert not re.search(r"\b(Bearer\s+[A-Za-z0-9._~+/=-]{6,}|sk-|ghp_)", rendered)
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
