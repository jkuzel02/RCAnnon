"""Shared OpenAI-compatible Chat Completions provider adapter."""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    InternalServerError,
    PermissionDeniedError,
    RateLimitError,
)
from pydantic import SecretStr

from rca.contracts.provider import (
    CompleteFn,
    PromptNotAllowedError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderResponse,
    ProviderResponseError,
    ProviderTimeoutError,
)
from rca.contracts.schemas import PROMPT_ALLOWLIST
from rca.providers.config import ProviderConfig

DEFAULT_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


def _scrub_secret(message: str, secret: str) -> str:
    if not secret:
        return message
    return message.replace(secret, "[REDACTED]")


def _as_optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _extract_token_counts(message: AIMessage) -> tuple[int | None, int | None, int | None]:
    """Extract nullable token counts without ever inventing missing values."""
    response_meta = message.response_metadata if isinstance(message.response_metadata, Mapping) else {}
    token_usage = response_meta.get("token_usage")
    if isinstance(token_usage, Mapping) and token_usage:
        return (
            _as_optional_int(token_usage.get("prompt_tokens")),
            _as_optional_int(token_usage.get("completion_tokens")),
            _as_optional_int(token_usage.get("total_tokens")),
        )

    usage_meta = message.usage_metadata
    if isinstance(usage_meta, Mapping) and usage_meta:
        # Only trust usage_metadata when response_metadata actually reported usage,
        # or when non-empty keys exist.
        return (
            _as_optional_int(usage_meta.get("input_tokens")),
            _as_optional_int(usage_meta.get("output_tokens")),
            _as_optional_int(usage_meta.get("total_tokens")),
        )
    return None, None, None


class OpenAIProvider:
    """Thread-safe OpenAI-compatible Chat Completions adapter using LangChain ChatOpenAI."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        prompts_dir: Path = DEFAULT_PROMPTS_DIR,
        prompt_allowlist: frozenset[str] = PROMPT_ALLOWLIST,
        sleep_fn: Callable[[float], None] = time.sleep,
        clock_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._prompts_dir = prompts_dir
        self._prompt_allowlist = prompt_allowlist
        self._sleep = sleep_fn
        self._clock = clock_fn
        self._prompt_cache: dict[str, str] = {}
        self._cache_lock = threading.Lock()

    @property
    def config(self) -> ProviderConfig:
        return self._config

    def _load_prompt(self, prompt_path: str) -> str:
        normalized = prompt_path.strip().lstrip("/")
        if normalized not in self._prompt_allowlist:
            raise PromptNotAllowedError(f"Prompt path is not in the allowlist: {prompt_path!r}.")
        with self._cache_lock:
            cached = self._prompt_cache.get(normalized)
            if cached is not None:
                return cached
        full_path = self._prompts_dir / normalized
        if not full_path.is_file():
            raise PromptNotAllowedError(f"Allowlisted prompt file not found: {normalized!r}.")
        text = full_path.read_text(encoding="utf-8")
        if not text.strip():
            raise ProviderResponseError(f"Prompt file is empty: {normalized!r}.")
        with self._cache_lock:
            self._prompt_cache[normalized] = text
        return text

    def _build_client(self, timeout_seconds: float) -> ChatOpenAI:
        kwargs: dict[str, Any] = {
            "model": self._config.model,
            "api_key": SecretStr(self._config.api_key),
            "base_url": self._config.base_url,
            "temperature": self._config.temperature,
            "timeout": timeout_seconds,
            "max_retries": 0,
            "streaming": False,
            "use_responses_api": False,
        }
        if self._config.max_output_tokens is not None:
            kwargs["max_tokens"] = self._config.max_output_tokens
        return ChatOpenAI(**kwargs)

    def _classify_error(self, error: Exception) -> tuple[ProviderError, bool]:
        """Map SDK errors to safe ProviderError subclasses and retry eligibility."""
        secret = self._config.api_key
        if isinstance(error, (AuthenticationError, PermissionDeniedError)):
            status = getattr(error, "status_code", None)
            return (
                ProviderAuthError(f"OpenAI API authentication failed (HTTP {status})."),
                False,
            )
        if isinstance(error, RateLimitError):
            return (
                ProviderRateLimitError("OpenAI API rate limit exceeded (HTTP 429)."),
                True,
            )
        if isinstance(error, (APITimeoutError, TimeoutError)):
            return (
                ProviderTimeoutError("OpenAI API request timed out."),
                True,
            )
        if isinstance(error, InternalServerError):
            status = getattr(error, "status_code", 500)
            return (
                ProviderError(f"OpenAI API server error (HTTP {status})."),
                True,
            )
        if isinstance(error, APIConnectionError):
            return (
                ProviderError("OpenAI API connection failed."),
                True,
            )
        if isinstance(error, APIStatusError):
            status = getattr(error, "status_code", None)
            retryable = status in RETRYABLE_STATUS_CODES
            if status in {401, 403}:
                return ProviderAuthError(f"OpenAI API authentication failed (HTTP {status})."), False
            if status == 429:
                return ProviderRateLimitError("OpenAI API rate limit exceeded (HTTP 429)."), True
            return (
                ProviderError(f"OpenAI API returned HTTP {status}."),
                retryable,
            )
        if isinstance(error, ProviderError):
            return error, False
        safe_detail = _scrub_secret(type(error).__name__, secret)
        return ProviderResponseError(f"Malformed provider response ({safe_detail})."), False

    def complete_response(
        self,
        prompt_path: str,
        payload: Mapping[str, Any],
    ) -> ProviderResponse:
        """Execute a bounded Chat Completions call and return typed metadata."""
        system_prompt = self._load_prompt(prompt_path)
        serialized_payload = json.dumps(payload, ensure_ascii=False)
        if (
            self._config.max_context_chars is not None
            and len(system_prompt) + len(serialized_payload) > self._config.max_context_chars
        ):
            raise ValueError(
                f"Combined prompt and payload exceeds max_context_chars "
                f"({self._config.max_context_chars})."
            )

        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=serialized_payload),
        ]

        started = self._clock()
        deadline = started + self._config.deadline_seconds
        max_attempts = 1 + self._config.max_retries
        last_error: ProviderError | None = None

        for attempt in range(1, max_attempts + 1):
            attempt_id = uuid.uuid4().hex
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise ProviderTimeoutError(
                    f"OpenAI API request exceeded overall deadline ({self._config.deadline_seconds}s)."
                )

            per_call_timeout = min(self._config.timeout_seconds, remaining)
            client = self._build_client(per_call_timeout)
            try:
                raw_message = client.invoke(messages)
            except Exception as exc:  # noqa: BLE001
                mapped, retryable = self._classify_error(exc)
                last_error = mapped
                if not retryable or attempt >= max_attempts:
                    raise mapped from None
                remaining_after = deadline - self._clock()
                if remaining_after <= 0:
                    raise ProviderTimeoutError(
                        f"OpenAI API request exceeded overall deadline "
                        f"({self._config.deadline_seconds}s)."
                    ) from None
                delay = min(
                    self._config.retry_backoff_seconds * (2 ** (attempt - 1)),
                    max(0.0, remaining_after),
                )
                if delay > 0:
                    self._sleep(delay)
                continue

            if not isinstance(raw_message, AIMessage):
                raise ProviderResponseError("Provider did not return an AIMessage.")

            response_meta = (
                raw_message.response_metadata
                if isinstance(raw_message.response_metadata, Mapping)
                else {}
            )
            finish_reason = response_meta.get("finish_reason")
            if finish_reason is not None and finish_reason != "stop":
                raise ProviderResponseError(
                    f"Model did not finish normally (finish_reason={finish_reason!r})."
                )

            content = raw_message.content
            if not isinstance(content, str) or not content.strip():
                raise ProviderResponseError("Model returned an empty or non-text completion.")

            input_tokens, output_tokens, total_tokens = _extract_token_counts(raw_message)
            request_id = raw_message.id or (
                str(response_meta["id"]) if response_meta.get("id") else None
            )
            model_name = str(response_meta.get("model_name") or self._config.model)

            return ProviderResponse(
                text=content,
                model=model_name,
                request_id=request_id,
                attempt_id=attempt_id,
                attempts=attempt,
                duration_seconds=round(self._clock() - started, 4),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                finish_status=str(finish_reason) if finish_reason is not None else None,
            )

        assert last_error is not None
        raise last_error

    def complete(self, prompt_path: str, payload: Mapping[str, Any]) -> str:
        """Thread-safe synchronous string completion matching the workflow test seam."""
        return self.complete_response(prompt_path, payload).text


def make_sync_complete(provider: OpenAIProvider) -> CompleteFn:
    """Return a thread-safe complete(prompt_path, payload) -> str callable."""
    return provider.complete
