"""Tests for Docker Compose packaging, ASGI server factory, UI gateway, and security contracts."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from rca.api import create_app_from_env
from rca.api.server import check_runtime_identity
from rca.api.ui_stub import create_ui_app_from_env
from rca.evidence import KubeconfigSecurityError

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "compose.yaml"
COMPOSE_EINFRA_FILE = REPO_ROOT / "infrastructure" / "compose" / "compose.einfra.yaml"
COMPOSE_KUBE_FILE = REPO_ROOT / "infrastructure" / "compose" / "compose.kube.yaml"
DOCKERFILE = REPO_ROOT / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"


def _write_secret_files(tmp_path: Path) -> tuple[Path, Path]:
    ui_tok = tmp_path / "ui.secret"
    op_tok = tmp_path / "op.secret"
    ui_tok.write_text("ui-compose-canary-token\n", encoding="utf-8")
    op_tok.write_text("op-compose-canary-token\n", encoding="utf-8")
    return ui_tok, op_tok


def _base_env(tmp_path: Path) -> dict[str, str]:
    ui_tok, op_tok = _write_secret_files(tmp_path)
    return {
        "RCANNON_PROVIDER_MODE": "mock",
        "RCANNON_UI_BEARER_TOKEN_FILE": str(ui_tok),
        "RCANNON_OPERATOR_BEARER_TOKEN_FILE": str(op_tok),
        "RCANNON_ALLOWED_SCOPES": '{"lab":["demo"]}',
        "RCANNON_DB_PATH": str(tmp_path / "data" / "rcannon.db"),
    }


def test_server_factory_mock_mode_works_without_llm_key_or_cluster(tmp_path: Path):
    env = _base_env(tmp_path)
    app = create_app_from_env(env)

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 200

        models = client.get(
            "/v1/models",
            headers={"Authorization": "Bearer ui-compose-canary-token"},
        )
        assert models.status_code == 200
        assert [m["id"] for m in models.json()["data"]] == [
            "rcannon-base",
            "rcannon-optimal",
            "rcannon-mas",
        ]

        # Without a configured target cluster, live run returns insufficient_evidence while health stays 200
        offline_run = client.post(
            "/api/v1/runs",
            headers={"Authorization": "Bearer op-compose-canary-token"},
            json={
                "variant": "base",
                "target": {"context_alias": "lab", "namespaces": ["demo"]},
                "incident": "checkout unreachable",
            },
        )
        assert offline_run.status_code == 200
        assert offline_run.json()["status"] == "insufficient_evidence"
        assert client.get("/healthz").status_code == 200

        # Bundle snapshot + all three variants succeed in mock mode
        snap_resp = client.post(
            "/api/v1/snapshots",
            headers={"Authorization": "Bearer op-compose-canary-token"},
            json={
                "bundle": {
                    "source_type": "bundle",
                    "context_alias": "lab",
                    "namespaces": ["demo"],
                    "context": {
                        "incident": "checkout OOMKilled",
                        "time_window": None,
                        "resources": [{"kind": "Pod", "metadata": {"name": "checkout-1"}}],
                        "events": [],
                        "logs": [],
                        "metrics": [],
                        "checks": [],
                    },
                }
            },
        )
        assert snap_resp.status_code == 201
        snapshot_id = snap_resp.json()["snapshot_id"]

        for model_name in ("rcannon-base", "rcannon-optimal", "rcannon-mas"):
            chat = client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer ui-compose-canary-token"},
                json={
                    "model": model_name,
                    "stream": model_name == "rcannon-mas",
                    "messages": [
                        {
                            "role": "user",
                            "content": f"/rca snapshot={snapshot_id} -- checkout OOMKilled",
                        }
                    ],
                },
            )
            assert chat.status_code == 200
            if model_name == "rcannon-mas":
                assert chat.headers["content-type"].startswith("text/event-stream")
                assert "[DONE]" in chat.text
                assert "## Cause" in chat.text
            else:
                assert chat.json()["choices"][0]["message"]["content"]


def test_server_factory_fails_closed_on_missing_or_invalid_secrets_and_kubeconfig(tmp_path: Path):
    env = _base_env(tmp_path)

    with pytest.raises(ValueError):
        create_app_from_env({k: v for k, v in env.items() if "UI_BEARER" not in k})

    with pytest.raises(ValueError):
        create_app_from_env({**env, "RCANNON_PROVIDER_MODE": "invalid-mode"})

    with pytest.raises(ValueError, match="Missing e-INFRA API key"):
        create_app_from_env({**env, "RCANNON_PROVIDER_MODE": "einfra"})

    missing_kube = tmp_path / "missing-kubeconfig"
    with pytest.raises(KubeconfigSecurityError):
        create_app_from_env(
            {
                **env,
                "RCANNON_KUBECONFIG_PATH": str(missing_kube),
                "RCANNON_KUBE_CONTEXT": "kind-local",
            }
        )

    exec_kube = tmp_path / "exec-kube.yaml"
    exec_kube.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "v1",
                "kind": "Config",
                "clusters": [
                    {
                        "name": "c",
                        "cluster": {"server": "https://host.docker.internal:6443"},
                    }
                ],
                "users": [{"name": "u", "user": {"exec": {"command": "/bin/sh"}}}],
                "contexts": [{"name": "kind-local", "context": {"cluster": "c", "user": "u"}}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(KubeconfigSecurityError, match="exec"):
        create_app_from_env(
            {
                **env,
                "RCANNON_KUBECONFIG_PATH": str(exec_kube),
                "RCANNON_KUBE_CONTEXT": "kind-local",
            }
        )


def test_relative_kubeconfig_ca_and_identity_check_succeed_and_state_survives_restart(tmp_path: Path):
    kube_dir = tmp_path / "kube"
    kube_dir.mkdir()
    (kube_dir / "ca.crt").write_text("-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----\n")
    kube_cfg = kube_dir / "config"
    kube_cfg.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "v1",
                "kind": "Config",
                "clusters": [
                    {
                        "name": "kind",
                        "cluster": {
                            "server": "https://host.docker.internal:6443",
                            "certificate-authority": "ca.crt",
                        },
                    }
                ],
                "users": [{"name": "readonly", "user": {"token": "synthetic-ro-token"}}],
                "contexts": [
                    {"name": "kind-local", "context": {"cluster": "kind", "user": "readonly"}}
                ],
            }
        ),
        encoding="utf-8",
    )

    env = {
        **_base_env(tmp_path),
        "RCANNON_KUBECONFIG_PATH": str(kube_cfg),
        "RCANNON_KUBE_CONTEXT": "kind-local",
        "RCANNON_TARGET_ALIAS": "lab",
    }
    identity = check_runtime_identity(env)
    assert identity["provider_mode"] == "mock"
    assert identity["collector_enabled"] is True
    assert identity["target_aliases"] == ["lab"]

    # Persistence across app restart on same RCANNON_DB_PATH
    app1 = create_app_from_env(_base_env(tmp_path))
    with TestClient(app1) as c1:
        created = c1.post(
            "/api/v1/snapshots",
            headers={"Authorization": "Bearer op-compose-canary-token"},
            json={
                "bundle": {
                    "source_type": "bundle",
                    "context_alias": "lab",
                    "namespaces": ["demo"],
                    "context": {
                        "incident": "restart check",
                        "time_window": None,
                        "resources": [{"kind": "Pod", "metadata": {"name": "p1"}}],
                        "events": [],
                        "logs": [],
                        "metrics": [],
                        "checks": [],
                    },
                }
            },
        )
        assert created.status_code == 201
        snap_id = created.json()["snapshot_id"]

        run_resp = c1.post(
            "/api/v1/runs",
            headers={"Authorization": "Bearer op-compose-canary-token"},
            json={"variant": "base", "snapshot_id": snap_id},
        )
        assert run_resp.status_code == 200
        run_id = run_resp.json()["run_id"]

    app2 = create_app_from_env(_base_env(tmp_path))
    with TestClient(app2) as c2:
        assert (
            c2.get(
                f"/api/v1/snapshots/{snap_id}",
                headers={"Authorization": "Bearer op-compose-canary-token"},
            ).status_code
            == 200
        )
        assert (
            c2.get(
                f"/api/v1/runs/{run_id}",
                headers={"Authorization": "Bearer op-compose-canary-token"},
            ).status_code
            == 200
        )


def test_ui_gateway_enforces_token_isolates_operator_routes_and_persists_state(tmp_path: Path):
    with pytest.raises(ValueError, match="UI service bearer token"):
        create_ui_app_from_env({})

    api_app = create_app_from_env(_base_env(tmp_path))
    ui_tok_file = tmp_path / "ui.secret"
    state_file = tmp_path / "ui-state" / "state.json"
    ui_env = {
        "RCANNON_API_URL": "http://api.internal",
        "RCANNON_UI_BEARER_TOKEN_FILE": str(ui_tok_file),
        "RCANNON_UI_STATE_PATH": str(state_file),
    }

    with TestClient(api_app):
        ui_app_1 = create_ui_app_from_env(ui_env, transport=httpx.ASGITransport(app=api_app))
        with TestClient(ui_app_1) as ui1:
            assert ui1.get("/healthz").status_code == 200
            assert ui1.get("/v1/models").status_code == 200
            assert ui1.get("/api/v1/runs").status_code == 404

            chat = ui1.post(
                "/v1/chat/completions",
                json={
                    "model": "rcannon-base",
                    "messages": [
                        {
                            "role": "user",
                            "content": "/rca context=lab namespace=demo -- ui-compose-canary-token check",
                        }
                    ],
                },
            )
            assert chat.status_code == 200

        # Restart UI gateway against same state path and verify history persists cleanly
        ui_app_2 = create_ui_app_from_env(ui_env, transport=httpx.ASGITransport(app=api_app))
        with TestClient(ui_app_2) as ui2:
            hist = ui2.get("/ui/history")
            assert hist.status_code == 200
            entries = hist.json()["entries"]
            assert len(entries) == 1
            assert "ui-compose-canary-token" not in json.dumps(entries)


def test_dockerfile_dockerignore_and_compose_security_posture():
    dockerfile_text = DOCKERFILE.read_text(encoding="utf-8")
    assert "USER 10001:10001" in dockerfile_text
    assert "uv sync --frozen --no-dev --no-editable" in dockerfile_text
    assert "HEALTHCHECK" in dockerfile_text
    assert "STOPSIGNAL SIGTERM" in dockerfile_text

    dockerignore_lines = {
        line.strip() for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines() if line.strip()
    }
    for required_ignore in (".git", ".env", "*.kubeconfig", "secrets", "infrastructure"):
        assert required_ignore in dockerignore_lines

    base = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    einfra = yaml.safe_load(COMPOSE_EINFRA_FILE.read_text(encoding="utf-8"))
    kube = yaml.safe_load(COMPOSE_KUBE_FILE.read_text(encoding="utf-8"))

    for doc in (base, einfra, kube):
        serialized = yaml.safe_dump(doc)
        assert "docker.sock" not in serialized
        assert "privileged: true" not in serialized

    api_svc = base["services"]["api"]
    ui_svc = base["services"]["ui"]
    mongo_svc = base["services"]["mongodb"]
    bootstrap_svc = base["services"]["ui-bootstrap"]

    assert api_svc["user"] == "10001:10001"
    assert api_svc["read_only"] is True
    assert "ALL" in api_svc["cap_drop"]
    assert "no-new-privileges:true" in api_svc["security_opt"]
    assert "ports" not in api_svc
    assert set(api_svc["networks"]) == {"ui_api", "api_egress", "telemetry_net"}
    assert set(api_svc["secrets"]) == {"rcannon_ui_bearer_token", "rcannon_operator_bearer_token"}

    assert "@sha256:" in ui_svc["image"]
    assert ui_svc["image"].startswith("registry.librechat.ai/librechat-ai/librechat:v0.8.9@sha256:")
    assert ui_svc["user"] == "1000:1000"
    assert ui_svc["read_only"] is True
    assert "ALL" in ui_svc["cap_drop"]
    assert "no-new-privileges:true" in ui_svc["security_opt"]
    assert ui_svc["ports"] == ["127.0.0.1:${RCANNON_UI_PORT:-3080}:3080"]
    assert set(ui_svc["networks"]) == {"ui_edge", "ui_api", "ui_db"}
    assert set(ui_svc["secrets"]) == {
        "rcannon_ui_bearer_token",
        "librechat_jwt_secret",
        "librechat_jwt_refresh_secret",
        "librechat_creds_key",
        "librechat_creds_iv",
    }
    assert "rcannon_operator_bearer_token" not in ui_svc["secrets"]
    assert "librechat_admin_password" not in ui_svc["secrets"]
    ui_env = ui_svc["environment"]
    assert ui_env["ENDPOINTS"] == "custom"
    assert ui_env["ALLOW_EMAIL_LOGIN"] == "true"
    assert ui_env["ALLOW_REGISTRATION"] == "false"
    assert ui_env["ALLOW_SOCIAL_LOGIN"] == "false"
    assert ui_env["ALLOW_SOCIAL_REGISTRATION"] == "false"
    assert ui_env["SEARCH"] == "false"

    assert mongo_svc["image"].startswith("mongo:8.0@sha256:")
    assert "ports" not in mongo_svc
    assert set(mongo_svc["networks"]) == {"ui_db"}
    assert "ALL" in mongo_svc["cap_drop"]
    assert "no-new-privileges:true" in mongo_svc["security_opt"]
    assert "rcannon_mongo_data:/data/db" in mongo_svc["volumes"]

    assert bootstrap_svc["profiles"] == ["bootstrap"]
    assert bootstrap_svc["restart"] == "no"
    assert bootstrap_svc["image"] == ui_svc["image"]
    assert bootstrap_svc["user"] == "1000:1000"
    assert bootstrap_svc["read_only"] is True
    assert "ALL" in bootstrap_svc["cap_drop"]
    assert "no-new-privileges:true" in bootstrap_svc["security_opt"]
    assert "ports" not in bootstrap_svc
    assert set(bootstrap_svc["networks"]) == {"ui_db"}
    assert "librechat_admin_password" in bootstrap_svc["secrets"]
    assert "rcannon_ui_bearer_token" not in bootstrap_svc["secrets"]
    assert "rcannon_operator_bearer_token" not in bootstrap_svc["secrets"]

    assert base["networks"]["ui_api"]["internal"] is True
    assert base["networks"]["ui_db"]["internal"] is True
    assert base["networks"]["telemetry_net"]["internal"] is True
    assert "rcannon_api_data" in base["volumes"]
    assert "rcannon_mongo_data" in base["volumes"]

    # Overlays scope LLM secret and kubeconfig mounts strictly to api
    assert list(einfra["services"].keys()) == ["api"]
    assert list(kube["services"].keys()) == ["api"]
    assert any(vol.endswith(":ro") for vol in kube["services"]["api"]["volumes"])
