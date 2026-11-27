#!/usr/bin/env python3
"""Recorded-incident OpenAI-compatible demo server and snapshot seeder."""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

CASE_PATH = Path("/demo/case.json")
TOKEN_PATH = Path("/run/rcannon-secrets/rcannon_operator_bearer_token")
ASSESSMENT = "Recorded demo response. The fixture supplies configuration evidence."


def _load_case() -> tuple[dict[str, Any], dict[str, Any]]:
    data = json.loads(CASE_PATH.read_text(encoding="utf-8"))
    return data["context"], data["root-cause"]


def _seed_snapshot() -> int:
    context, _ = _load_case()
    token = TOKEN_PATH.read_text(encoding="utf-8").strip()
    req = urllib.request.Request(
        "http://rcannon:8000/api/v1/snapshots",
        data=json.dumps({"bundle": {"source_type": "fixture", "context": context}}).encode(
            "utf-8"
        ),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    snapshot_id = body.get("snapshot_id", "")
    if not re.fullmatch(r"snap_[a-f0-9]+", snapshot_id):
        raise RuntimeError("The sample incident has no snapshot ID.")
    print(snapshot_id)
    return 0


def _build_report(example: dict[str, Any]) -> dict[str, Any]:
    return {
        **example,
        "cause": f"Recorded demo report. {example['cause']}",
        "supporting_evidence": [
            "The Deployment/api manifest sets the web container memory limit to 64Mi.",
            "The same container sets ALLOCATE_MIB to 128.",
        ],
        "remediation": (
            "Raise the web container memory limit to 256Mi to accommodate its working set "
            "and runtime overhead."
        ),
        "recovery_check": (
            "Confirm that the container reaches readiness and remains running without "
            "further memory-related restarts."
        ),
    }


def _completion_for(payload: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    if "context" in payload:
        return report
    if "logs" in payload:
        return {
            "agent": "logs-connectivity",
            "log_findings": [],
            "connectivity_findings": [],
            "runtime_failure_mode": "unknown",
            "preliminary_assessment": ASSESSMENT,
        }
    if "metrics" in payload:
        return {
            "agent": "metrics-telemetry",
            "resource_findings": [],
            "throttling_detected": False,
            "saturation_identified": None,
            "preliminary_assessment": ASSESSMENT,
        }
    return {
        "agent": "k8s-config-events",
        "anomalies": [],
        "suspicious_resources": [],
        "significant_events": [],
        "preliminary_assessment": ASSESSMENT,
    }


def _serve() -> int:
    _, example = _load_case()
    report = _build_report(example)

    class Handler(BaseHTTPRequestHandler):
        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            encoded = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/healthz":
                self._send_json(200, {"status": "ok"})
                return
            self._send_json(404, {"error": {"message": "Not found."}})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/v1/chat/completions":
                self._send_json(404, {"error": {"message": "Not found."}})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 1024 * 1024:
                    raise ValueError("Demo request size out of bounds.")
                raw = self.rfile.read(length)
                body = json.loads(raw.decode("utf-8"))
                payload = json.loads(body["messages"][-1]["content"])
                completion = _completion_for(payload, report)
                self._send_json(
                    200,
                    {
                        "id": "recorded-incident",
                        "object": "chat.completion",
                        "created": 0,
                        "model": "recorded-incident",
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "stop",
                                "message": {
                                    "role": "assistant",
                                    "content": json.dumps(completion),
                                },
                            }
                        ],
                    },
                )
            except Exception:  # noqa: BLE001
                self._send_json(400, {"error": {"message": "Invalid demo request."}})

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            del format, args

    ThreadingHTTPServer(("0.0.0.0", 8081), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    if "--seed" in sys.argv[1:]:
        raise SystemExit(_seed_snapshot())
    raise SystemExit(_serve())
