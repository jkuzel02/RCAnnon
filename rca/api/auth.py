"""Role-separated Bearer token authentication for UI and operator routes."""

from __future__ import annotations

import hmac
from typing import Literal

from fastapi import HTTPException, Request

from rca.api.config import ApiConfig

AuthRole = Literal["ui", "operator"]


def _matches_any(token: str, candidates: frozenset[str]) -> bool:
    matched = False
    for candidate in candidates:
        matched |= hmac.compare_digest(token, candidate)
    return matched


def _extract_bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "code": "unauthorized",
                    "message": "Missing or invalid Bearer token.",
                }
            },
        )
    parts = authorization.strip().split()
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "code": "unauthorized",
                    "message": "Missing or invalid Bearer token.",
                }
            },
        )
    return parts[1]


def authenticate_token(
    authorization: str | None,
    config: ApiConfig,
    required_role: AuthRole,
) -> AuthRole:
    """Validate Bearer token and enforce route-level UI vs operator separation."""
    token = _extract_bearer_token(authorization)
    if _matches_any(token, config.ui_tokens):
        role: AuthRole = "ui"
    elif _matches_any(token, config.operator_tokens):
        role = "operator"
    else:
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "code": "unauthorized",
                    "message": "Missing or invalid Bearer token.",
                }
            },
        )

    if role != required_role:
        raise HTTPException(
            status_code=403,
            detail={
                "error": {
                    "code": "forbidden",
                    "message": "Token is not authorized for this route.",
                }
            },
        )
    return role


def require_ui_auth(request: Request) -> AuthRole:
    """FastAPI dependency enforcing UI-scoped Bearer authentication."""
    return authenticate_token(
        request.headers.get("Authorization"),
        request.app.state.config,
        "ui",
    )


def require_operator_auth(request: Request) -> AuthRole:
    """FastAPI dependency enforcing operator-scoped Bearer authentication."""
    return authenticate_token(
        request.headers.get("Authorization"),
        request.app.state.config,
        "operator",
    )
