"""Shared contracts and schemas for RCAnnon."""

from .provider import (
    CompleteFn,
    PromptNotAllowedError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderResponse,
    ProviderResponseError,
    ProviderTimeoutError,
)
from .schemas import (
    CASE,
    PROMPT_ALLOWLIST,
    RCA,
    REPORT_SCHEMAS,
    TEXT,
    array,
    object_with,
)

__all__ = [
    "CASE",
    "CompleteFn",
    "PROMPT_ALLOWLIST",
    "PromptNotAllowedError",
    "ProviderAuthError",
    "ProviderError",
    "ProviderRateLimitError",
    "ProviderResponse",
    "ProviderResponseError",
    "ProviderTimeoutError",
    "RCA",
    "REPORT_SCHEMAS",
    "TEXT",
    "array",
    "object_with",
]
