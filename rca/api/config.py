"""Runtime configuration for the RCAnnon FastAPI service."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DB_PATH = Path("rcannon_api.db")
DEFAULT_SNAPSHOT_TTL_SECONDS = 3600
DEFAULT_MAX_SNAPSHOTS = 100
DEFAULT_MAX_RUNS = 500
DEFAULT_MAX_BUNDLE_BYTES = 256 * 1024
DEFAULT_MAX_PROMPT_CHARS = 4000
DEFAULT_MAX_WORKERS = 4
DEFAULT_RUN_DEADLINE_SECONDS = 90.0


def _read_tokens(env: Mapping[str, str], var_name: str, file_var_name: str) -> frozenset[str]:
    raw_value = env.get(var_name, "").strip()
    if not raw_value:
        file_path = env.get(file_var_name, "").strip()
        if file_path:
            path = Path(file_path)
            if not path.is_file():
                raise ValueError(f"Token file referenced by {file_var_name} does not exist.")
            raw_value = path.read_text(encoding="utf-8").strip()
    tokens = frozenset(part.strip() for part in raw_value.split(",") if part.strip())
    if not tokens:
        raise ValueError(f"Missing bearer token configuration: set {var_name} or {file_var_name}.")
    return tokens


def _parse_positive_int(raw: str | None, default: int, name: str) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError as error:
        raise ValueError(f"{name} must be an integer.") from error
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero.")
    return value


def _parse_positive_float(raw: str | None, default: float, name: str) -> float:
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError as error:
        raise ValueError(f"{name} must be a number.") from error
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero.")
    return value


def _parse_allowed_scopes(raw: str | None) -> dict[str, frozenset[str]]:
    if raw is None or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("RCANNON_ALLOWED_SCOPES must be valid JSON.") from error
    if not isinstance(parsed, dict):
        raise ValueError("RCANNON_ALLOWED_SCOPES must be a JSON object.")
    scopes: dict[str, frozenset[str]] = {}
    for alias, namespaces in parsed.items():
        if not isinstance(alias, str) or not alias.strip():
            raise ValueError("Scope alias names must be non-empty strings.")
        if not isinstance(namespaces, list) or not all(
            isinstance(ns, str) and ns.strip() for ns in namespaces
        ):
            raise ValueError(
                f"Namespaces for scope {alias!r} must be a list of non-empty strings."
            )
        scopes[alias.strip()] = frozenset(ns.strip() for ns in namespaces)
    return scopes


@dataclass(frozen=True)
class ApiConfig:
    """Validated configuration for the FastAPI and OpenAI-compatible facade."""

    ui_tokens: frozenset[str] = field(repr=False)
    operator_tokens: frozenset[str] = field(repr=False)
    allowed_scopes: dict[str, frozenset[str]] = field(default_factory=dict)
    db_path: Path = DEFAULT_DB_PATH
    snapshot_ttl_seconds: int = DEFAULT_SNAPSHOT_TTL_SECONDS
    max_snapshots: int = DEFAULT_MAX_SNAPSHOTS
    max_runs: int = DEFAULT_MAX_RUNS
    max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES
    max_prompt_chars: int = DEFAULT_MAX_PROMPT_CHARS
    max_workers: int = DEFAULT_MAX_WORKERS
    run_deadline_seconds: float = DEFAULT_RUN_DEADLINE_SECONDS
    extra_redaction_secrets: tuple[str, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if not self.ui_tokens or any(not tok.strip() for tok in self.ui_tokens):
            raise ValueError("ui_tokens must contain at least one non-empty token.")
        if not self.operator_tokens or any(not tok.strip() for tok in self.operator_tokens):
            raise ValueError("operator_tokens must contain at least one non-empty token.")
        if not self.ui_tokens.isdisjoint(self.operator_tokens):
            raise ValueError("UI and operator bearer tokens must be distinct.")
        if self.snapshot_ttl_seconds <= 0:
            raise ValueError("snapshot_ttl_seconds must be greater than zero.")
        if self.max_snapshots <= 0:
            raise ValueError("max_snapshots must be greater than zero.")
        if self.max_runs <= 0:
            raise ValueError("max_runs must be greater than zero.")
        if self.max_bundle_bytes <= 0:
            raise ValueError("max_bundle_bytes must be greater than zero.")
        if self.max_prompt_chars <= 0:
            raise ValueError("max_prompt_chars must be greater than zero.")
        if self.max_workers <= 0:
            raise ValueError("max_workers must be greater than zero.")
        if self.run_deadline_seconds <= 0:
            raise ValueError("run_deadline_seconds must be greater than zero.")

    def is_scope_allowed(self, context_alias: str, namespaces: Iterable[str]) -> bool:
        """Return True iff context_alias and all namespaces are explicitly allowlisted."""
        allowed_ns = self.allowed_scopes.get(context_alias)
        if allowed_ns is None:
            return False
        ns_list = list(namespaces)
        if not ns_list:
            return False
        return all(ns in allowed_ns for ns in ns_list)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ApiConfig:
        """Build ApiConfig from environment variables and optional token files."""
        env = os.environ if environ is None else environ
        ui_tokens = _read_tokens(
            env,
            "RCANNON_UI_BEARER_TOKEN",
            "RCANNON_UI_BEARER_TOKEN_FILE",
        )
        operator_tokens = _read_tokens(
            env,
            "RCANNON_OPERATOR_BEARER_TOKEN",
            "RCANNON_OPERATOR_BEARER_TOKEN_FILE",
        )
        allowed_scopes = _parse_allowed_scopes(env.get("RCANNON_ALLOWED_SCOPES"))
        raw_db_path = env.get("RCANNON_DB_PATH", "").strip()
        db_path = Path(raw_db_path) if raw_db_path else DEFAULT_DB_PATH

        return cls(
            ui_tokens=ui_tokens,
            operator_tokens=operator_tokens,
            allowed_scopes=allowed_scopes,
            db_path=db_path,
            snapshot_ttl_seconds=_parse_positive_int(
                env.get("RCANNON_SNAPSHOT_TTL_SECONDS"),
                DEFAULT_SNAPSHOT_TTL_SECONDS,
                "RCANNON_SNAPSHOT_TTL_SECONDS",
            ),
            max_snapshots=_parse_positive_int(
                env.get("RCANNON_MAX_SNAPSHOTS"),
                DEFAULT_MAX_SNAPSHOTS,
                "RCANNON_MAX_SNAPSHOTS",
            ),
            max_runs=_parse_positive_int(
                env.get("RCANNON_MAX_RUNS"),
                DEFAULT_MAX_RUNS,
                "RCANNON_MAX_RUNS",
            ),
            max_bundle_bytes=_parse_positive_int(
                env.get("RCANNON_MAX_BUNDLE_BYTES"),
                DEFAULT_MAX_BUNDLE_BYTES,
                "RCANNON_MAX_BUNDLE_BYTES",
            ),
            max_prompt_chars=_parse_positive_int(
                env.get("RCANNON_MAX_PROMPT_CHARS"),
                DEFAULT_MAX_PROMPT_CHARS,
                "RCANNON_MAX_PROMPT_CHARS",
            ),
            max_workers=_parse_positive_int(
                env.get("RCANNON_MAX_WORKERS"),
                DEFAULT_MAX_WORKERS,
                "RCANNON_MAX_WORKERS",
            ),
            run_deadline_seconds=_parse_positive_float(
                env.get("RCANNON_RUN_DEADLINE_SECONDS"),
                DEFAULT_RUN_DEADLINE_SECONDS,
                "RCANNON_RUN_DEADLINE_SECONDS",
            ),
        )
