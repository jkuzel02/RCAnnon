#!/usr/bin/env python3
"""Initialize isolated RCAnnon API bearer tokens for Docker Compose without host setup.

Never prints any secret values to stdout or stderr.
"""

from __future__ import annotations

import os
import secrets
import tempfile
from pathlib import Path


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


def main() -> int:
    seed_dir = Path(os.environ.get("SEED_DIR", "/seed-secrets"))
    api_out_dir = Path(os.environ.get("API_OUT_DIR", "/out/api"))
    api_uid = int(os.environ.get("API_UID", "10001"))
    api_gid = int(os.environ.get("API_GID", "10001"))
    skip_chown = os.environ.get("SKIP_CHOWN", "") == "1"

    _ensure_dir(api_out_dir, api_uid, api_gid, skip_chown=skip_chown)

    ui_bearer_token = (
        _read_nonempty_file(api_out_dir / "rcannon_ui_bearer_token")
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

    print("Initialized isolated Docker Compose API runtime secrets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
