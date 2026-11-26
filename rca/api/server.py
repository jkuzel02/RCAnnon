"""Container and CLI ASGI entrypoint wiring API config, provider, and collector."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from rca.api.app import create_app
from rca.api.config import ApiConfig
from rca.contracts.provider import CompleteFn, PromptNotAllowedError
from rca.contracts.runtime import CollectorFn
from rca.contracts.schemas import PROMPT_ALLOWLIST
from rca.evidence import KubernetesEvidenceCollector, TargetClusterConfig, build_collector_fn
from rca.providers import OpenAIProvider, ProviderConfig

_VALID_PROVIDER_MODES = frozenset({"openai", "mock"})


def _read_optional_secret(
    env: Mapping[str, str],
    var_name: str,
    file_var_name: str,
) -> str | None:
    raw = env.get(var_name, "").strip()
    if raw:
        return raw
    file_path = env.get(file_var_name, "").strip()
    if not file_path:
        return None
    path = Path(file_path)
    if not path.is_file():
        raise ValueError(f"Secret file referenced by {file_var_name} does not exist.")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"Secret file referenced by {file_var_name} must not be empty.")
    return value


def mock_complete(prompt_path: str, payload: Mapping[str, Any]) -> str:
    """Deterministic offline completion callable for mock-mode Compose smoke tests."""
    if prompt_path not in PROMPT_ALLOWLIST:
        raise PromptNotAllowedError(f"Prompt {prompt_path!r} is outside PROMPT_ALLOWLIST.")

    if prompt_path == "base.md":
        incident = ""
        ctx = payload.get("context")
        if isinstance(ctx, Mapping):
            incident = str(ctx.get("incident") or "").strip()
        summary = f" for '{incident}'" if incident else ""
        return (
            f"[mock-base] Offline diagnosis{summary}: inspect workload events, resource limits, "
            "and recent container terminations in the scoped namespace."
        )

    if prompt_path == "mas/k8s-config-events.md":
        return json.dumps(
            {
                "agent": "k8s-config-events",
                "anomalies": [],
                "suspicious_resources": [],
                "significant_events": [],
                "preliminary_assessment": "Mock configuration and events assessment completed.",
            }
        )

    if prompt_path == "mas/logs-connectivity.md":
        return json.dumps(
            {
                "agent": "logs-connectivity",
                "log_findings": [],
                "connectivity_findings": [],
                "runtime_failure_mode": "mock_offline_mode",
                "preliminary_assessment": "Mock logs and connectivity assessment completed.",
            }
        )

    if prompt_path == "mas/metrics-telemetry.md":
        return json.dumps(
            {
                "agent": "metrics-telemetry",
                "resource_findings": [],
                "throttling_detected": False,
                "saturation_identified": None,
                "preliminary_assessment": "Mock metrics assessment completed.",
            }
        )

    return json.dumps(
        {
            "cause": "Mock offline diagnosis completed without live LLM inference.",
            "causal_chain": "Evidence snapshot passed validation and ran through the offline mock provider.",
            "supporting_evidence": ["Mock provider mode (RCANNON_PROVIDER_MODE=mock)"],
            "remediation": "Switch to RCANNON_PROVIDER_MODE=openai with a valid OpenAI API key for live analysis.",
            "recovery_check": "Confirm service /readyz returns 200 and live completions succeed.",
        }
    )


def _resolve_provider(env: Mapping[str, str]) -> tuple[str, CompleteFn, tuple[str, ...]]:
    mode = env.get("RCANNON_PROVIDER_MODE", "openai").strip().lower() or "openai"
    if mode not in _VALID_PROVIDER_MODES:
        raise ValueError(
            f"Unsupported RCANNON_PROVIDER_MODE {mode!r}. Expected 'openai' or 'mock'."
        )
    if mode == "mock":
        return mode, mock_complete, ()

    provider_cfg = ProviderConfig.from_env(env)
    provider = OpenAIProvider(provider_cfg)
    return mode, provider.complete, (provider_cfg.api_key,)


def _build_collector_from_env(
    env: Mapping[str, str],
    api_config: ApiConfig,
) -> tuple[CollectorFn | None, tuple[str, ...], list[str]]:
    kubeconfig_raw = env.get("RCANNON_KUBECONFIG_PATH", "").strip()
    targets_file_raw = env.get("RCANNON_TARGETS_CONFIG_FILE", "").strip()
    if not kubeconfig_raw and not targets_file_raw:
        return None, (), []

    if kubeconfig_raw and targets_file_raw:
        raise ValueError(
            "Configure either RCANNON_KUBECONFIG_PATH or RCANNON_TARGETS_CONFIG_FILE, not both."
        )

    collector = KubernetesEvidenceCollector()
    registered_aliases: list[str] = []
    extra_secrets: list[str] = []

    if kubeconfig_raw:
        kube_context = env.get("RCANNON_KUBE_CONTEXT", "").strip()
        if not kube_context:
            raise ValueError(
                "RCANNON_KUBE_CONTEXT is required when RCANNON_KUBECONFIG_PATH is set."
            )
        alias = env.get("RCANNON_TARGET_ALIAS", "lab").strip() or "lab"
        allowed_ns = api_config.allowed_scopes.get(alias)
        if not allowed_ns:
            raise ValueError(
                f"RCANNON_ALLOWED_SCOPES must allowlist at least one namespace for alias {alias!r}."
            )
        prom_url = env.get("RCANNON_PROMETHEUS_URL", "").strip() or None
        prom_ca_raw = env.get("RCANNON_PROMETHEUS_CA_FILE", "").strip()
        prom_ca = Path(prom_ca_raw) if prom_ca_raw else None
        if prom_ca is not None and not prom_ca.is_file():
            raise ValueError("RCANNON_PROMETHEUS_CA_FILE does not exist.")
        prom_token = _read_optional_secret(
            env,
            "RCANNON_PROMETHEUS_BEARER_TOKEN",
            "RCANNON_PROMETHEUS_BEARER_TOKEN_FILE",
        )
        if prom_token:
            extra_secrets.append(prom_token)

        collector.register_target(
            TargetClusterConfig(
                context_alias=alias,
                allowed_namespaces=allowed_ns,
                kubeconfig_path=Path(kubeconfig_raw),
                context_name=kube_context,
                prometheus_url=prom_url,
                prometheus_ca_file=prom_ca,
                prometheus_bearer_token=prom_token,
            )
        )
        registered_aliases.append(alias)
    else:
        targets_path = Path(targets_file_raw)
        if not targets_path.is_file():
            raise ValueError("RCANNON_TARGETS_CONFIG_FILE does not exist.")
        raw_data = json.loads(targets_path.read_text(encoding="utf-8"))
        entries = raw_data.get("targets") if isinstance(raw_data, dict) else raw_data
        if not isinstance(entries, list) or not entries:
            raise ValueError("RCANNON_TARGETS_CONFIG_FILE must contain a non-empty list of targets.")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("Each target entry in RCANNON_TARGETS_CONFIG_FILE must be an object.")
            alias = str(entry.get("context_alias", "")).strip()
            kube_path = str(entry.get("kubeconfig_path", "")).strip()
            kube_context = str(entry.get("context_name", "")).strip()
            if not alias or not kube_path or not kube_context:
                raise ValueError(
                    "Target entries require context_alias, kubeconfig_path, and context_name."
                )
            allowed_ns = api_config.allowed_scopes.get(alias)
            if not allowed_ns:
                raise ValueError(
                    f"Target alias {alias!r} must have allowlisted namespaces in RCANNON_ALLOWED_SCOPES."
                )
            prom_ca_raw = str(entry.get("prometheus_ca_file", "") or "").strip()
            prom_ca = Path(prom_ca_raw) if prom_ca_raw else None
            if prom_ca is not None and not prom_ca.is_file():
                raise ValueError(f"prometheus_ca_file for {alias!r} does not exist.")
            collector.register_target(
                TargetClusterConfig(
                    context_alias=alias,
                    allowed_namespaces=allowed_ns,
                    kubeconfig_path=Path(kube_path),
                    context_name=kube_context,
                    prometheus_url=str(entry.get("prometheus_url", "") or "").strip() or None,
                    prometheus_ca_file=prom_ca,
                )
            )
            registered_aliases.append(alias)

    return build_collector_fn(collector), tuple(extra_secrets), registered_aliases


def check_runtime_identity(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Validate runtime configuration, secrets, and optional kubeconfig without serving HTTP."""
    env = os.environ if environ is None else environ
    api_config = ApiConfig.from_env(env)
    provider_mode, _complete, _provider_secrets = _resolve_provider(env)
    collector_fn, _collector_secrets, target_aliases = _build_collector_from_env(env, api_config)
    return {
        "uid": os.getuid() if hasattr(os, "getuid") else None,
        "gid": os.getgid() if hasattr(os, "getgid") else None,
        "provider_mode": provider_mode,
        "allowed_scopes": {k: sorted(v) for k, v in sorted(api_config.allowed_scopes.items())},
        "target_aliases": target_aliases,
        "collector_enabled": collector_fn is not None,
        "db_path": str(api_config.db_path),
    }


def create_app_from_env(environ: Mapping[str, str] | None = None) -> FastAPI:
    """ASGI factory building the scoped RCAnnon FastAPI service from environment/secrets."""
    env = os.environ if environ is None else environ
    base_config = ApiConfig.from_env(env)
    provider_mode, complete_fn, provider_secrets = _resolve_provider(env)
    collector_fn, collector_secrets, _aliases = _build_collector_from_env(env, base_config)

    merged_secrets = (*base_config.extra_redaction_secrets, *provider_secrets, *collector_secrets)
    api_config = ApiConfig(
        ui_tokens=base_config.ui_tokens,
        operator_tokens=base_config.operator_tokens,
        allowed_scopes=base_config.allowed_scopes,
        db_path=base_config.db_path,
        snapshot_ttl_seconds=base_config.snapshot_ttl_seconds,
        max_snapshots=base_config.max_snapshots,
        max_runs=base_config.max_runs,
        max_bundle_bytes=base_config.max_bundle_bytes,
        max_prompt_chars=base_config.max_prompt_chars,
        max_workers=base_config.max_workers,
        run_deadline_seconds=base_config.run_deadline_seconds,
        extra_redaction_secrets=merged_secrets,
    )
    api_config.db_path.parent.mkdir(parents=True, exist_ok=True)
    app = create_app(config=api_config, complete=complete_fn, collector=collector_fn)
    app.state.provider_mode = provider_mode
    return app


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RCAnnon API runtime configuration check.")
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate configuration, secret files, and optional kubeconfig, then print safe summary JSON.",
    )
    args = parser.parse_args(argv)
    if args.check_config:
        summary = check_runtime_identity()
        print(json.dumps(summary, indent=2))
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
