#!/usr/bin/env python3
"""Initialize isolated per-service runtime secrets for Docker Compose without host setup.

Never prints any secret values to stdout or stderr.
"""

from __future__ import annotations

import os
import secrets
import tempfile
from pathlib import Path

DEFAULT_ADMIN_PASSWORD = "rcannon-local-admin"


def _read_nonempty_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    value = path.read_text(encoding="utf-8").strip()
    return value or None


def _ensure_dir(path: Path, uid: int, gid: int, *, skip_chown: bool) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o750)
    if not skip_chown:
        os.chown(path, uid, gid)


def _write_secret(path: Path, value: str, uid: int, gid: int, *, skip_chown: bool) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp.", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{value}\n")
        os.chmod(tmp_path, 0o400)
        if not skip_chown:
            os.chown(tmp_path, uid, gid)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _resolve_admin_password(seed_dir: Path, bootstrap_out_dir: Path) -> str:
    raw_env = os.environ.get("LIBRECHAT_ADMIN_PASSWORD")
    if raw_env is not None and raw_env != "":
        if "\n" in raw_env or "\r" in raw_env:
            raise SystemExit("LIBRECHAT_ADMIN_PASSWORD must be a single line.")
        stripped = raw_env.strip()
        if stripped:
            if len(stripped) < 12:
                raise SystemExit("LIBRECHAT_ADMIN_PASSWORD must be at least 12 characters.")
            return stripped

    return (
        _read_nonempty_file(bootstrap_out_dir / "librechat_admin_password")
        or _read_nonempty_file(seed_dir / "librechat_admin_password.secret")
        or DEFAULT_ADMIN_PASSWORD
    )


def main() -> int:
    seed_dir = Path(os.environ.get("SEED_DIR", "/seed-secrets"))
    api_out_dir = Path(os.environ.get("API_OUT_DIR", "/out/api"))
    ui_out_dir = Path(os.environ.get("UI_OUT_DIR", "/out/ui"))
    bootstrap_out_dir = Path(os.environ.get("BOOTSTRAP_OUT_DIR", "/out/bootstrap"))

    api_uid = int(os.environ.get("API_UID", "10001"))
    api_gid = int(os.environ.get("API_GID", "10001"))
    ui_uid = int(os.environ.get("UI_UID", "1000"))
    ui_gid = int(os.environ.get("UI_GID", "1000"))
    skip_chown = os.environ.get("SKIP_CHOWN", "") == "1"

    _ensure_dir(api_out_dir, api_uid, api_gid, skip_chown=skip_chown)
    _ensure_dir(ui_out_dir, ui_uid, ui_gid, skip_chown=skip_chown)
    _ensure_dir(bootstrap_out_dir, ui_uid, ui_gid, skip_chown=skip_chown)

    ui_bearer_token = (
        _read_nonempty_file(api_out_dir / "rcannon_ui_bearer_token")
        or _read_nonempty_file(ui_out_dir / "rcannon_ui_bearer_token")
        or _read_nonempty_file(seed_dir / "ui_bearer_token.secret")
        or secrets.token_hex(32)
    )
    _write_secret(
        api_out_dir / "rcannon_ui_bearer_token",
        ui_bearer_token,
        api_uid,
        api_gid,
        skip_chown=skip_chown,
    )
    _write_secret(
        ui_out_dir / "rcannon_ui_bearer_token",
        ui_bearer_token,
        ui_uid,
        ui_gid,
        skip_chown=skip_chown,
    )

    operator_bearer_token = (
        _read_nonempty_file(api_out_dir / "rcannon_operator_bearer_token")
        or _read_nonempty_file(seed_dir / "operator_bearer_token.secret")
        or secrets.token_hex(32)
    )
    while operator_bearer_token == ui_bearer_token:
        operator_bearer_token = secrets.token_hex(32)
    _write_secret(
        api_out_dir / "rcannon_operator_bearer_token",
        operator_bearer_token,
        api_uid,
        api_gid,
        skip_chown=skip_chown,
    )

    librechat_specs = (
        ("librechat_jwt_secret", 32),
        ("librechat_jwt_refresh_secret", 32),
        ("librechat_creds_key", 32),
        ("librechat_creds_iv", 16),
    )
    resolved_librechat: dict[str, str] = {}
    for name, nbytes in librechat_specs:
        value = (
            _read_nonempty_file(ui_out_dir / name)
            or _read_nonempty_file(bootstrap_out_dir / name)
            or _read_nonempty_file(seed_dir / f"{name}.secret")
            or secrets.token_hex(nbytes)
        )
        if name == "librechat_jwt_refresh_secret":
            while value == resolved_librechat["librechat_jwt_secret"]:
                value = secrets.token_hex(nbytes)
        resolved_librechat[name] = value
        _write_secret(
            ui_out_dir / name,
            value,
            ui_uid,
            ui_gid,
            skip_chown=skip_chown,
        )
        _write_secret(
            bootstrap_out_dir / name,
            value,
            ui_uid,
            ui_gid,
            skip_chown=skip_chown,
        )

    admin_password = _resolve_admin_password(seed_dir, bootstrap_out_dir)
    _write_secret(
        bootstrap_out_dir / "librechat_admin_password",
        admin_password,
        ui_uid,
        ui_gid,
        skip_chown=skip_chown,
    )
    print("Initialized isolated Docker Compose runtime secrets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
