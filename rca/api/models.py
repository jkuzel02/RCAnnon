"""Strict request and response models for the OpenAI facade and operator API."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Any, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rca.contracts.runtime import (
    SnapshotSourceType,
    SourceCoverage,
    TargetSelector,
    VariantName,
)
from rca.contracts.schemas import CASE

OPENAI_MODEL_TO_VARIANT: dict[str, VariantName] = {
    "rcannon-base": "base",
    "rcannon-optimal": "optimal",
    "rcannon-mas": "mas",
}

_ALLOWED_DIRECTIVE_KEYS = frozenset(
    {"context", "namespace", "namespaces", "workload", "snapshot"}
)
_SNAPSHOT_ID_RE = re.compile(r"[A-Za-z0-9_-]{4,128}")
_CONTEXT_VALIDATOR = Draft202012Validator(CASE["properties"]["context"])


class ChatMessage(BaseModel):
    """Text-only OpenAI chat message; rejects attachments and multimodal lists."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant"]
    content: str
    name: str | None = None


class ChatCompletionRequest(BaseModel):
    """Scoped OpenAI /v1/chat/completions payload rejecting client overrides and tools."""

    model_config = ConfigDict(extra="forbid")

    model: Literal["rcannon-base", "rcannon-optimal", "rcannon-mas"]
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    user: str | None = None
    stream_options: dict[str, Any] | None = None


@dataclass(frozen=True)
class ParsedChatDirective:
    """Validated target or snapshot reference extracted from the latest user message."""

    target: TargetSelector | None
    snapshot_id: str | None
    incident: str | None


def parse_chat_directive(
    messages: list[ChatMessage],
    *,
    max_prompt_chars: int,
) -> ParsedChatDirective:
    """Extract and validate an explicit /rca scope or snapshot directive."""
    user_messages = [msg.content for msg in messages if msg.role == "user"]
    if not user_messages:
        raise ValueError("At least one user message is required.")

    latest = user_messages[-1].strip()
    if not latest:
        raise ValueError("Latest user message must not be empty.")
    if len(latest) > max_prompt_chars:
        raise ValueError(f"Prompt exceeds maximum length of {max_prompt_chars} characters.")

    match = re.match(r"^/rca(?:\s+(.*))?$", latest, re.IGNORECASE | re.DOTALL)
    if match is None:
        raise ValueError(
            "Request must start with '/rca context=<alias> namespace=<ns> -- <incident>' "
            "or '/rca snapshot=<id>'."
        )
    rest = (match.group(1) or "").strip()
    if not rest:
        raise ValueError("Missing scope or snapshot arguments after '/rca'.")

    header, _, incident_part = rest.partition("--")
    try:
        tokens = shlex.split(header)
    except ValueError as error:
        raise ValueError("Malformed /rca directive arguments.") from error

    if not tokens:
        raise ValueError("Missing scope or snapshot arguments before '--'.")

    parsed: dict[str, str] = {}
    for token in tokens:
        if "=" not in token:
            raise ValueError(f"Directive token must use key=value format: {token!r}.")
        key, value = token.split("=", 1)
        key = key.strip().lower()
        value = value.strip()
        if key not in _ALLOWED_DIRECTIVE_KEYS:
            raise ValueError(f"Unsupported directive option: {key!r}.")
        if not value:
            raise ValueError(f"Directive option {key!r} must not be empty.")
        if key in parsed:
            raise ValueError(f"Duplicate directive option: {key!r}.")
        parsed[key] = value

    incident = incident_part.strip() or None

    if "snapshot" in parsed:
        if len(parsed) > 1:
            raise ValueError("snapshot cannot be combined with context, namespace, or workload.")
        snapshot_id = parsed["snapshot"]
        if not _SNAPSHOT_ID_RE.fullmatch(snapshot_id):
            raise ValueError("Invalid snapshot identifier format.")
        return ParsedChatDirective(target=None, snapshot_id=snapshot_id, incident=incident)

    if "namespace" in parsed and "namespaces" in parsed:
        raise ValueError("Specify either namespace or namespaces, not both.")

    context_alias = parsed.get("context")
    raw_ns = parsed.get("namespace") or parsed.get("namespaces")
    if not context_alias or not raw_ns:
        raise ValueError("Both context and namespace(s) are required.")
    if incident is None:
        raise ValueError("An incident description after '--' is required.")

    namespaces = [part.strip() for part in raw_ns.split(",") if part.strip()]
    if not namespaces:
        raise ValueError("At least one namespace is required.")

    target = TargetSelector(
        context_alias=context_alias,
        namespaces=namespaces,
        workload_selector=parsed.get("workload"),
    )
    return ParsedChatDirective(target=target, snapshot_id=None, incident=incident)


class BundleInput(BaseModel):
    """Sanitized pre-collected evidence bundle submitted by an operator."""

    model_config = ConfigDict(extra="forbid")

    context: dict[str, Any]
    coverage: dict[str, SourceCoverage] | None = None
    context_alias: str | None = None
    namespaces: list[str] = Field(default_factory=list)
    observed_at: str | None = None
    source_type: Literal["bundle", "fixture"] = "bundle"

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


class CreateSnapshotRequest(BaseModel):
    """Operator snapshot registration request from either a bundle or a live target."""

    model_config = ConfigDict(extra="forbid")

    bundle: BundleInput | None = None
    target: TargetSelector | None = None
    incident: str | None = None

    @model_validator(mode="after")
    def _validate_choice(self) -> CreateSnapshotRequest:
        if (self.bundle is None) == (self.target is None):
            raise ValueError("Provide exactly one of 'bundle' or 'target'.")
        if self.target is not None and (not self.incident or not self.incident.strip()):
            raise ValueError("A non-empty 'incident' is required when collecting from 'target'.")
        if self.incident is not None:
            self.incident = self.incident.strip() or None
        return self


class SnapshotSummary(BaseModel):
    """Metadata returned when creating or inspecting a registered snapshot."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["v1"] = "v1"
    snapshot_id: str
    scope_hash: str
    source_type: SnapshotSourceType
    context_alias: str | None = None
    namespaces: list[str] = Field(default_factory=list)
    collected_at: str
    observed_at: str | None = None
    expires_at: str
    coverage: dict[str, SourceCoverage]
    has_usable_evidence: bool


class CreateRunRequest(BaseModel):
    """Operator diagnosis execution request using either a snapshot_id or live target."""

    model_config = ConfigDict(extra="forbid")

    variant: VariantName
    snapshot_id: str | None = None
    target: TargetSelector | None = None
    incident: str | None = None

    @model_validator(mode="after")
    def _validate_choice(self) -> CreateRunRequest:
        if (self.snapshot_id is None) == (self.target is None):
            raise ValueError("Provide exactly one of 'snapshot_id' or 'target'.")
        if self.snapshot_id is not None and not _SNAPSHOT_ID_RE.fullmatch(self.snapshot_id):
            raise ValueError("Invalid snapshot_id format.")
        if self.target is not None and (not self.incident or not self.incident.strip()):
            raise ValueError("A non-empty 'incident' is required when running against 'target'.")
        if self.incident is not None:
            self.incident = self.incident.strip() or None
        return self
