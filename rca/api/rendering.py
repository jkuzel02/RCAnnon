"""Defensive secret redaction and safe Markdown rendering for API responses."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from rca.contracts.runtime import RunResult

_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{6,}")
_API_TOKEN_RE = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{10,})")
_PEM_BLOCK_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]+PRIVATE KEY-----.*?-----END [A-Z0-9 ]+PRIVATE KEY-----",
    re.DOTALL,
)
_KUBECONFIG_CERT_RE = re.compile(
    r"(client-certificate-data|client-key-data|certificate-authority-data)\s*:\s*\S+"
)
_KV_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|password|passwd|secret|token)\s*[:=]\s*(['\"]?)([^\s'\",;]+)\2"
)


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Scrub literal secrets and common credential patterns from text."""
    cleaned = text
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "[REDACTED]")
    cleaned = _PEM_BLOCK_RE.sub("[REDACTED_KEY]", cleaned)
    cleaned = _KUBECONFIG_CERT_RE.sub(r"\1: [REDACTED]", cleaned)
    cleaned = _BEARER_RE.sub("Bearer [REDACTED]", cleaned)
    cleaned = _API_TOKEN_RE.sub("[REDACTED]", cleaned)
    cleaned = _KV_SECRET_RE.sub(r"\1=[REDACTED]", cleaned)
    return cleaned


def redact_value(value: Any, secrets: Iterable[str] = ()) -> Any:
    """Recursively redact strings inside dicts and lists without mutating the input."""
    secret_tuple = tuple(s for s in secrets if s)
    if isinstance(value, str):
        return redact_text(value, secret_tuple)
    if isinstance(value, list):
        return [redact_value(item, secret_tuple) for item in value]
    if isinstance(value, tuple):
        return [redact_value(item, secret_tuple) for item in value]
    if isinstance(value, dict):
        return {
            redact_value(k, secret_tuple): redact_value(v, secret_tuple)
            for k, v in value.items()
        }
    return value


def sanitize_markdown_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Redact credentials and neutralize raw HTML tags before Markdown rendering."""
    redacted = redact_text(text, secrets)
    return redacted.replace("<", "&lt;").replace(">", "&gt;")


def _format_limitations(limitations: list[str], secrets: tuple[str, ...]) -> list[str]:
    if not limitations:
        return []
    lines = ["## Coverage Limitations", ""]
    for item in limitations:
        lines.append(f"- {sanitize_markdown_text(item, secrets)}")
    return lines


def render_run_markdown(run: RunResult, secrets: Iterable[str] = ()) -> str:
    """Render a finalized RunResult into safe Markdown for OpenAI chat completions."""
    secret_tuple = tuple(s for s in secrets if s)
    sections: list[str] = [
        f"**Run:** `{sanitize_markdown_text(run.run_id, secret_tuple)}` "
        f"| **Variant:** `{run.variant}` | **Status:** `{run.status}`"
    ]

    if run.status == "insufficient_evidence":
        sections.append("## Insufficient Evidence")
        if run.limitations:
            sections.append(
                "\n".join(f"- {sanitize_markdown_text(item, secret_tuple)}" for item in run.limitations)
            )
        else:
            sections.append("No usable Kubernetes evidence was available to perform diagnosis.")
        return "\n\n".join(sections)

    if run.status in {"failed", "interrupted"}:
        sections.append("## Run Status")
        code = sanitize_markdown_text(run.error_code or run.status, secret_tuple)
        msg = sanitize_markdown_text(
            run.error_message or "Diagnosis did not complete.",
            secret_tuple,
        )
        sections.append(f"- **Code:** `{code}`\n- **Detail:** {msg}")
        return "\n\n".join(sections)

    if isinstance(run.result, dict):
        evidence_items = run.result.get("supporting_evidence", [])
        evidence_bullets = "\n".join(
            f"- {sanitize_markdown_text(str(item), secret_tuple)}"
            for item in evidence_items
        )
        sections.extend(
            [
                f"## Cause\n\n{sanitize_markdown_text(str(run.result.get('cause', '')), secret_tuple)}",
                f"## Causal Chain\n\n{sanitize_markdown_text(str(run.result.get('causal_chain', '')), secret_tuple)}",
                f"## Supporting Evidence\n\n{evidence_bullets}",
                f"## Remediation\n\n{sanitize_markdown_text(str(run.result.get('remediation', '')), secret_tuple)}",
                f"## Recovery Check\n\n{sanitize_markdown_text(str(run.result.get('recovery_check', '')), secret_tuple)}",
            ]
        )
    elif isinstance(run.result, str):
        sections.append(sanitize_markdown_text(run.result, secret_tuple))

    limitation_lines = _format_limitations(run.limitations, secret_tuple)
    if limitation_lines:
        sections.append("\n".join(limitation_lines))

    return "\n\n".join(sections)
