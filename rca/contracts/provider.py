"""Typed provider response models and safe error hierarchy."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

CompleteFn = Callable[[str, Mapping[str, Any]], str]


@dataclass(frozen=True)
class ProviderResponse:
    """Structured result from a single Chat Completions call."""

    text: str
    model: str
    request_id: str | None
    attempt_id: str
    attempts: int
    duration_seconds: float
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    finish_status: str | None


class ProviderError(RuntimeError):
    """Base error for provider failures with credential-safe messages."""


class ProviderAuthError(ProviderError):
    """Raised on HTTP 401/403 authentication or authorization failures."""


class ProviderRateLimitError(ProviderError):
    """Raised on HTTP 429 throttling failures."""


class ProviderTimeoutError(ProviderError, TimeoutError):
    """Raised when a provider request exceeds its timeout or overall deadline."""


class ProviderResponseError(ProviderError, ValueError):
    """Raised when the provider returns malformed, empty, or truncated output."""


class PromptNotAllowedError(ValueError):
    """Raised when a caller requests a prompt path outside the fixed allowlist."""
