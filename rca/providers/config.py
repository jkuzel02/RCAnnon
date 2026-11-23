"""Configuration loading for the shared e-INFRA Chat Completions provider."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_BASE_URL = "https://llm.ai.e-infra.cz/v1"
DEFAULT_MODEL = "deepseek-v4.1-flash"
DEFAULT_TEMPERATURE = 0.0
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_DEADLINE_SECONDS = 90.0
DEFAULT_MAX_RETRIES = 0
DEFAULT_RETRY_BACKOFF_SECONDS = 0.25


def _parse_optional_int(raw: str | None, name: str) -> int | None:
    if raw is None or not raw.strip():
        return None
    try:
        value = int(raw.strip())
    except ValueError as error:
        raise ValueError(f"{name} must be an integer.") from error
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero.")
    return value


def _parse_float(raw: str | None, default: float, name: str, *, allow_zero: bool = False) -> float:
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError as error:
        raise ValueError(f"{name} must be a number.") from error
    if allow_zero:
        if value < 0:
            raise ValueError(f"{name} must be non-negative.")
    elif value <= 0:
        raise ValueError(f"{name} must be greater than zero.")
    return value


def _parse_non_negative_int(raw: str | None, default: int, name: str) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError as error:
        raise ValueError(f"{name} must be an integer.") from error
    if value < 0:
        raise ValueError(f"{name} must be non-negative.")
    return value


def _load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _resolve_api_key(env: Mapping[str, str]) -> str:
    for key_var in ("LLM_AI_EINFRA_API_KEY", "EINFRA_AI_TOKEN"):
        value = env.get(key_var, "").strip()
        if value:
            return value
    for file_var in ("LLM_AI_EINFRA_API_KEY_FILE", "EINFRA_AI_TOKEN_FILE"):
        raw_path = env.get(file_var, "").strip()
        if raw_path:
            secret_path = Path(raw_path)
            if not secret_path.is_file():
                raise ValueError(f"Secret file referenced by {file_var} does not exist.")
            value = secret_path.read_text(encoding="utf-8").strip()
            if value:
                return value
    raise ValueError(
        "Missing e-INFRA API key. Set LLM_AI_EINFRA_API_KEY, EINFRA_AI_TOKEN, "
        "or LLM_AI_EINFRA_API_KEY_FILE."
    )


@dataclass(frozen=True)
class ProviderConfig:
    """Validated runtime configuration for the e-INFRA Chat Completions adapter."""

    api_key: str = field(repr=False)
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    temperature: float = DEFAULT_TEMPERATURE
    max_output_tokens: int | None = None
    max_context_chars: int | None = None
    max_concurrency: int | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS

    def __post_init__(self) -> None:
        if not self.api_key or not self.api_key.strip():
            raise ValueError("api_key must not be empty.")
        if not self.base_url or not self.base_url.strip():
            raise ValueError("base_url must not be empty.")
        if not self.model or not self.model.strip():
            raise ValueError("model must not be empty.")
        if self.temperature < 0:
            raise ValueError("temperature must be non-negative.")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero.")
        if self.deadline_seconds <= 0:
            raise ValueError("deadline_seconds must be greater than zero.")
        if self.max_retries < 0:
            raise ValueError("max_retries must be non-negative.")
        if self.retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds must be non-negative.")
        if self.max_output_tokens is not None and self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be greater than zero.")
        if self.max_context_chars is not None and self.max_context_chars <= 0:
            raise ValueError("max_context_chars must be greater than zero.")
        if self.max_concurrency is not None and self.max_concurrency <= 0:
            raise ValueError("max_concurrency must be greater than zero.")

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        env_file: Path | None = None,
    ) -> ProviderConfig:
        """Build ProviderConfig from environment variables and optional .env/secret files."""
        merged: dict[str, str] = {}
        if env_file is not None:
            merged.update(_load_env_file(env_file))
        source = os.environ if environ is None else environ
        merged.update(source)

        api_key = _resolve_api_key(merged)
        base_url = (
            merged.get("LLM_AI_EINFRA_BASE_URL", "").strip()
            or merged.get("EINFRA_AI_BASE_URL", "").strip()
            or DEFAULT_BASE_URL
        ).rstrip("/")
        model = (
            merged.get("LLM_AI_EINFRA_MODEL", "").strip()
            or merged.get("EINFRA_AI_MODEL", "").strip()
            or DEFAULT_MODEL
        )
        temperature = _parse_float(
            merged.get("LLM_AI_EINFRA_TEMPERATURE"),
            DEFAULT_TEMPERATURE,
            "LLM_AI_EINFRA_TEMPERATURE",
            allow_zero=True,
        )
        max_output_tokens = _parse_optional_int(
            merged.get("LLM_AI_EINFRA_MAX_OUTPUT_TOKENS"),
            "LLM_AI_EINFRA_MAX_OUTPUT_TOKENS",
        )
        max_context_chars = _parse_optional_int(
            merged.get("LLM_AI_EINFRA_MAX_CONTEXT_CHARS"),
            "LLM_AI_EINFRA_MAX_CONTEXT_CHARS",
        )
        max_concurrency = _parse_optional_int(
            merged.get("LLM_AI_EINFRA_MAX_CONCURRENCY"),
            "LLM_AI_EINFRA_MAX_CONCURRENCY",
        )
        timeout_seconds = _parse_float(
            merged.get("LLM_AI_EINFRA_TIMEOUT_SECONDS"),
            DEFAULT_TIMEOUT_SECONDS,
            "LLM_AI_EINFRA_TIMEOUT_SECONDS",
        )
        deadline_seconds = _parse_float(
            merged.get("LLM_AI_EINFRA_DEADLINE_SECONDS"),
            DEFAULT_DEADLINE_SECONDS,
            "LLM_AI_EINFRA_DEADLINE_SECONDS",
        )
        max_retries = _parse_non_negative_int(
            merged.get("LLM_AI_EINFRA_MAX_RETRIES"),
            DEFAULT_MAX_RETRIES,
            "LLM_AI_EINFRA_MAX_RETRIES",
        )
        retry_backoff_seconds = _parse_float(
            merged.get("LLM_AI_EINFRA_RETRY_BACKOFF_SECONDS"),
            DEFAULT_RETRY_BACKOFF_SECONDS,
            "LLM_AI_EINFRA_RETRY_BACKOFF_SECONDS",
            allow_zero=True,
        )
        return cls(
            api_key=api_key,
            base_url=base_url,
            model=model,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            max_context_chars=max_context_chars,
            max_concurrency=max_concurrency,
            timeout_seconds=timeout_seconds,
            deadline_seconds=deadline_seconds,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
        )
