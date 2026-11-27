"""Tests for LibreChat v0.8.9 config hardening, secret initialization, entrypoint, and admin bootstrap."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
LIBRECHAT_CONFIG = REPO_ROOT / "infrastructure" / "compose" / "librechat.yaml"
INIT_SECRETS_SCRIPT = REPO_ROOT / "infrastructure" / "compose" / "scripts" / "init-dev-secrets.sh"
INIT_COMPOSE_SECRETS_SCRIPT = (
    REPO_ROOT / "infrastructure" / "compose" / "scripts" / "init-compose-secrets.py"
)
ENTRYPOINT_SCRIPT = REPO_ROOT / "infrastructure" / "compose" / "scripts" / "librechat-entrypoint.sh"
BOOTSTRAP_SCRIPT = REPO_ROOT / "infrastructure" / "compose" / "scripts" / "bootstrap-librechat-admin.sh"

_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


def _write_valid_librechat_secrets(secrets_dir: Path) -> dict[str, Path]:
    secrets_dir.mkdir(parents=True, exist_ok=True)
    values = {
        "RCANNON_UI_BEARER_TOKEN_FILE": ("rcannon_ui_bearer_token", "ui-secret-canary-value"),
        "LIBRECHAT_JWT_SECRET_FILE": ("librechat_jwt_secret", "jwt-secret-canary-value"),
        "LIBRECHAT_JWT_REFRESH_SECRET_FILE": (
            "librechat_jwt_refresh_secret",
            "jwt-refresh-canary-value",
        ),
        "LIBRECHAT_CREDS_KEY_FILE": ("librechat_creds_key", "a" * 64),
        "LIBRECHAT_CREDS_IV_FILE": ("librechat_creds_iv", "b" * 32),
        "LIBRECHAT_ADMIN_PASSWORD_FILE": (
            "librechat_admin_password",
            "AdminCanaryPassword12345!",
        ),
    }
    paths: dict[str, Path] = {}
    for env_key, (filename, secret_val) in values.items():
        path = secrets_dir / filename
        path.write_text(secret_val + "\n", encoding="utf-8")
        paths[env_key] = path
    return paths


def test_librechat_config_hardening_and_single_endpoint():
    cfg = yaml.safe_load(LIBRECHAT_CONFIG.read_text(encoding="utf-8"))

    assert str(cfg["version"]) == "1.3.19"
    assert cfg["cache"] is True
    assert cfg["registration"]["socialLogins"] == []

    iface = cfg["interface"]
    assert iface["modelSelect"] is True
    for disabled_flag in (
        "parameters",
        "presets",
        "multiConvo",
        "bookmarks",
        "memories",
        "prompts",
        "agents",
        "temporaryChat",
        "runCode",
        "webSearch",
        "fileSearch",
        "fileCitations",
        "skills",
        "schedules",
        "sharedLinks",
    ):
        assert iface[disabled_flag] is False
    assert iface["marketplace"]["use"] is False
    assert iface["mcpServers"] == {"use": False, "create": False, "share": False, "public": False}
    assert iface["remoteAgents"] == {"use": False, "create": False, "share": False, "public": False}
    assert iface["peoplePicker"] == {"users": False, "groups": False, "roles": False}

    file_cfg = cfg["fileConfig"]
    assert file_cfg["serverFileSizeLimit"] == 0
    assert file_cfg["endpoints"]["default"]["disabled"] is True
    assert file_cfg["endpoints"]["RCAnnon"]["disabled"] is True

    endpoints = cfg["endpoints"]
    assert endpoints["allowedAddresses"] == ["rcannon:8000"]
    assert endpoints["all"]["titleConvo"] is False
    assert len(endpoints["custom"]) == 1

    ep = endpoints["custom"][0]
    assert ep["name"] == "RCAnnon"
    assert ep["apiKey"] == "${RCANNON_UI_BEARER_TOKEN}"
    assert ep["baseURL"] == "http://rcannon:8000/v1"
    assert ep["models"] == {
        "default": ["rcannon-base", "rcannon-optimal", "rcannon-mas"],
        "fetch": False,
    }
    assert ep["titleConvo"] is False
    assert {"temperature", "top_p", "tools", "tool_choice", "max_tokens", "max_completion_tokens"}.issubset(
        set(ep["dropParams"])
    )


def test_init_dev_secrets_generates_required_files_idempotently(tmp_path: Path):
    scripts_dir = tmp_path / "scripts"
    secrets_dir = tmp_path / "secrets"
    scripts_dir.mkdir()
    secrets_dir.mkdir()
    target_script = scripts_dir / "init-dev-secrets.sh"
    shutil.copy2(INIT_SECRETS_SCRIPT, target_script)

    first = subprocess.run(
        ["bash", str(target_script)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=True,
    )
    expected_lengths = {
        "ui_bearer_token.secret": 64,
        "operator_bearer_token.secret": 64,
        "librechat_jwt_secret.secret": 64,
        "librechat_jwt_refresh_secret.secret": 64,
        "librechat_creds_key.secret": 64,
        "librechat_creds_iv.secret": 32,
        "librechat_admin_password.secret": 48,
    }
    values: dict[str, str] = {}
    for name, expected_len in expected_lengths.items():
        fpath = secrets_dir / name
        assert fpath.is_file()
        val = fpath.read_text(encoding="utf-8").strip()
        assert len(val) == expected_len
        assert _HEX_RE.fullmatch(val)
        values[name] = val
        assert val not in first.stdout
        assert val not in first.stderr

    assert values["ui_bearer_token.secret"] != values["operator_bearer_token.secret"]
    assert values["librechat_jwt_secret.secret"] != values["librechat_jwt_refresh_secret.secret"]

    second = subprocess.run(
        ["bash", str(target_script)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=True,
    )
    for name, prev_val in values.items():
        assert (secrets_dir / name).read_text(encoding="utf-8").strip() == prev_val
        assert f"Keeping existing {name}" in second.stdout


def test_librechat_entrypoint_fails_closed_and_exports_validated_secrets(tmp_path: Path):
    secret_env = _write_valid_librechat_secrets(tmp_path / "secrets")
    base_env = {**os.environ, **{k: str(v) for k, v in secret_env.items()}}

    # Valid secrets export cleanly to the child process without printing values
    ok = subprocess.run(
        [
            "sh",
            str(ENTRYPOINT_SCRIPT),
            "env",
        ],
        env=base_env,
        text=True,
        capture_output=True,
        check=True,
    )
    exported = dict(line.split("=", 1) for line in ok.stdout.splitlines() if "=" in line)
    assert exported["RCANNON_UI_BEARER_TOKEN"] == "ui-secret-canary-value"
    assert exported["JWT_SECRET"] == "jwt-secret-canary-value"
    assert exported["JWT_REFRESH_SECRET"] == "jwt-refresh-canary-value"
    assert exported["CREDS_KEY"] == "a" * 64
    assert exported["CREDS_IV"] == "b" * 32

    # Missing file fails closed
    secret_env["LIBRECHAT_JWT_SECRET_FILE"].unlink()
    missing = subprocess.run(["sh", str(ENTRYPOINT_SCRIPT), "true"], env=base_env, text=True, capture_output=True)
    assert missing.returncode != 0
    secret_env["LIBRECHAT_JWT_SECRET_FILE"].write_text("jwt-secret-canary-value\n", encoding="utf-8")

    # Wrong length / non-hex CREDS_KEY fails closed without echoing secret
    bad_canary = "z" * 64
    secret_env["LIBRECHAT_CREDS_KEY_FILE"].write_text(bad_canary + "\n", encoding="utf-8")
    bad_hex = subprocess.run(["sh", str(ENTRYPOINT_SCRIPT), "true"], env=base_env, text=True, capture_output=True)
    assert bad_hex.returncode != 0
    assert bad_canary not in bad_hex.stderr

    short_canary = "ab" * 8
    secret_env["LIBRECHAT_CREDS_IV_FILE"].write_text(short_canary + "\n", encoding="utf-8")
    bad_len = subprocess.run(["sh", str(ENTRYPOINT_SCRIPT), "true"], env=base_env, text=True, capture_output=True)
    assert bad_len.returncode != 0
    assert short_canary not in bad_len.stderr


def test_bootstrap_script_validation_idempotence_and_conflict_handling(tmp_path: Path):
    secret_env = _write_valid_librechat_secrets(tmp_path / "secrets")
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    state_file = tmp_path / "mongo_state.json"

    # Fake node & npm stubs to exercise real shell logic without Docker
    fake_node = fake_bin / "node"
    fake_node.write_text(
        f"""#!/usr/bin/env python3
import json, sys
from pathlib import Path

script = sys.argv[2] if len(sys.argv) > 2 else ""
state_path = Path({str(state_file)!r})

if "const fs = require" in script:
    mode = sys.argv[3]
    state = json.loads(sys.stdin.read())
    if mode == "before":
        if state["totalUsers"] == 0:
            print("CREATE")
        elif (
            state["emailExists"]
            and state["usernameExists"]
            and state["sameRecord"]
            and state["emailRole"] == "ADMIN"
            and state["emailVerified"] is True
        ):
            print("IDEMPOTENT")
        else:
            print("CONFLICT")
    elif mode == "after":
        ok = (
            state["totalUsers"] == 1
            and state["adminCount"] == 1
            and state["emailExists"]
            and state["usernameExists"]
            and state["sameRecord"]
            and state["emailRole"] == "ADMIN"
            and state["emailVerified"] is True
        )
        sys.exit(0 if ok else 1)
else:
    print("__RCANNON_STATE__=" + state_path.read_text(encoding="utf-8"))
""",
        encoding="utf-8",
    )
    fake_node.chmod(0o755)

    argv_capture = tmp_path / "npm_argv.json"
    stdin_capture = tmp_path / "npm_stdin.txt"
    fake_npm = fake_bin / "npm"
    fake_npm.write_text(
        f"""#!/usr/bin/env python3
import json, sys
from pathlib import Path

Path({str(argv_capture)!r}).write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
Path({str(stdin_capture)!r}).write_text(sys.stdin.read(), encoding="utf-8")
Path({str(state_file)!r}).write_text(json.dumps({{
    "totalUsers": 1,
    "adminCount": 1,
    "emailExists": True,
    "usernameExists": True,
    "sameRecord": True,
    "emailRole": "ADMIN",
    "usernameRole": "ADMIN",
    "emailVerified": True,
}}), encoding="utf-8")
""",
        encoding="utf-8",
    )
    fake_npm.chmod(0o755)

    env = {
        **os.environ,
        **{k: str(v) for k, v in secret_env.items()},
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "MONGO_URI": "mongodb://mongodb:27017/LibreChat",
        "LIBRECHAT_APP_DIR": str(app_dir),
        "LIBRECHAT_ADMIN_EMAIL": "admin@rcannon.local",
        "LIBRECHAT_ADMIN_NAME": "RCAnnon Admin",
        "LIBRECHAT_ADMIN_USERNAME": "rcannon-admin",
    }

    # 1. Fresh DB invokes create-user with password over stdin only (never in argv)
    state_file.write_text(
        json.dumps(
            {
                "totalUsers": 0,
                "adminCount": 0,
                "emailExists": False,
                "usernameExists": False,
                "sameRecord": False,
                "emailRole": None,
                "usernameRole": None,
                "emailVerified": False,
            }
        ),
        encoding="utf-8",
    )
    created = subprocess.run(["sh", str(BOOTSTRAP_SCRIPT)], env=env, text=True, capture_output=True)
    assert created.returncode == 0, created.stderr
    assert "AdminCanaryPassword12345!" not in json.loads(argv_capture.read_text(encoding="utf-8"))
    assert "AdminCanaryPassword12345!" not in created.stdout
    assert "AdminCanaryPassword12345!" not in created.stderr
    assert stdin_capture.read_text(encoding="utf-8") == "AdminCanaryPassword12345!\n"

    # 2. Existing verified ADMIN succeeds idempotently
    again = subprocess.run(["sh", str(BOOTSTRAP_SCRIPT)], env=env, text=True, capture_output=True)
    assert again.returncode == 0
    assert "already satisfied" in again.stdout

    # 3. Conflict / wrong role fails closed with upstream recovery commands
    state_file.write_text(
        json.dumps(
            {
                "totalUsers": 1,
                "adminCount": 0,
                "emailExists": True,
                "usernameExists": True,
                "sameRecord": True,
                "emailRole": "USER",
                "usernameRole": "USER",
                "emailVerified": True,
            }
        ),
        encoding="utf-8",
    )
    conflict = subprocess.run(["sh", str(BOOTSTRAP_SCRIPT)], env=env, text=True, capture_output=True)
    assert conflict.returncode != 0
    assert "npm run reset-password" in conflict.stderr

    # 4. Fail-closed credential & identity validation
    secret_env["LIBRECHAT_ADMIN_PASSWORD_FILE"].write_text("short\n", encoding="utf-8")
    weak = subprocess.run(["sh", str(BOOTSTRAP_SCRIPT)], env=env, text=True, capture_output=True)
    assert weak.returncode != 0
    assert "short" not in weak.stderr

    secret_env["LIBRECHAT_ADMIN_PASSWORD_FILE"].write_text("line1\nline2\n", encoding="utf-8")
    multi = subprocess.run(["sh", str(BOOTSTRAP_SCRIPT)], env=env, text=True, capture_output=True)
    assert multi.returncode != 0

    secret_env["LIBRECHAT_ADMIN_PASSWORD_FILE"].write_text("AdminCanaryPassword12345!\n", encoding="utf-8")
    bad_email = subprocess.run(
        ["sh", str(BOOTSTRAP_SCRIPT)],
        env={**env, "LIBRECHAT_ADMIN_EMAIL": "not-an-email"},
        text=True,
        capture_output=True,
    )
    assert bad_email.returncode != 0


def test_init_compose_secrets_isolates_volumes_is_idempotent_and_validates_password(tmp_path: Path):
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    api_dir = tmp_path / "out" / "api"
    ui_dir = tmp_path / "out" / "ui"
    bootstrap_dir = tmp_path / "out" / "bootstrap"

    env = {
        **os.environ,
        "SEED_DIR": str(seed_dir),
        "API_OUT_DIR": str(api_dir),
        "UI_OUT_DIR": str(ui_dir),
        "BOOTSTRAP_OUT_DIR": str(bootstrap_dir),
        "SKIP_CHOWN": "1",
        "LIBRECHAT_ADMIN_PASSWORD": "CustomCanaryPassword123",
    }

    api_init = subprocess.run(
        [sys.executable, str(INIT_COMPOSE_SECRETS_SCRIPT), "api"],
        env={**env, "LIBRECHAT_ADMIN_PASSWORD": "shortpw"},
        text=True,
        capture_output=True,
        check=True,
    )
    assert not ui_dir.exists()
    assert not bootstrap_dir.exists()
    first = subprocess.run(
        [sys.executable, str(INIT_COMPOSE_SECRETS_SCRIPT), "librechat"],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    api_files = {p.name for p in api_dir.iterdir()}
    ui_files = {p.name for p in ui_dir.iterdir()}
    bootstrap_files = {p.name for p in bootstrap_dir.iterdir()}

    assert api_files == {"rcannon_ui_bearer_token", "rcannon_operator_bearer_token"}
    assert ui_files == {
        "rcannon_ui_bearer_token",
        "librechat_jwt_secret",
        "librechat_jwt_refresh_secret",
        "librechat_creds_key",
        "librechat_creds_iv",
    }
    assert bootstrap_files == {
        "librechat_admin_password",
        "librechat_jwt_secret",
        "librechat_jwt_refresh_secret",
        "librechat_creds_key",
        "librechat_creds_iv",
    }

    ui_tok = (api_dir / "rcannon_ui_bearer_token").read_text(encoding="utf-8").strip()
    op_tok = (api_dir / "rcannon_operator_bearer_token").read_text(encoding="utf-8").strip()
    assert ui_tok == (ui_dir / "rcannon_ui_bearer_token").read_text(encoding="utf-8").strip()
    assert ui_tok != op_tok
    assert (
        (bootstrap_dir / "librechat_admin_password").read_text(encoding="utf-8").strip()
        == "CustomCanaryPassword123"
    )
    for secret_val in (ui_tok, op_tok, "CustomCanaryPassword123"):
        assert secret_val not in api_init.stdout
        assert secret_val not in api_init.stderr
        assert secret_val not in first.stdout
        assert secret_val not in first.stderr

    # Re-run preserves existing generated secrets
    jwt_before = (ui_dir / "librechat_jwt_secret").read_text(encoding="utf-8").strip()
    subprocess.run(
        [sys.executable, str(INIT_COMPOSE_SECRETS_SCRIPT), "librechat"],
        env={k: v for k, v in env.items() if k != "LIBRECHAT_ADMIN_PASSWORD"},
        text=True,
        capture_output=True,
        check=True,
    )
    assert (api_dir / "rcannon_ui_bearer_token").read_text(encoding="utf-8").strip() == ui_tok
    assert (ui_dir / "librechat_jwt_secret").read_text(encoding="utf-8").strip() == jwt_before

    # Short or multiline LIBRECHAT_ADMIN_PASSWORD fails closed without echoing
    for bad_pw in ("shortpw", "line1\nline2-long-enough"):
        failed = subprocess.run(
            [sys.executable, str(INIT_COMPOSE_SECRETS_SCRIPT), "librechat"],
            env={**env, "LIBRECHAT_ADMIN_PASSWORD": bad_pw},
            text=True,
            capture_output=True,
        )
        assert failed.returncode != 0
        assert bad_pw not in failed.stdout
        assert bad_pw not in failed.stderr
