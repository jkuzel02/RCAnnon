"""Minimal non-root loopback UI gateway stub for Compose smoke verification before #14."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from rca.api.rendering import redact_text

DEFAULT_API_URL = "http://api:8000"
DEFAULT_UI_STATE_PATH = Path("/var/lib/rcannon-ui/state.json")
DEFAULT_UI_TIMEOUT_SECONDS = 95.0
MAX_UI_HISTORY_ENTRIES = 50


def _read_ui_token(env: Mapping[str, str]) -> str:
    raw = env.get("RCANNON_UI_BEARER_TOKEN", "").strip()
    if raw:
        return raw
    file_path = env.get("RCANNON_UI_BEARER_TOKEN_FILE", "").strip()
    if file_path:
        path = Path(file_path)
        if not path.is_file():
            raise ValueError("Token file referenced by RCANNON_UI_BEARER_TOKEN_FILE does not exist.")
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    raise ValueError(
        "Missing UI service bearer token: set RCANNON_UI_BEARER_TOKEN or RCANNON_UI_BEARER_TOKEN_FILE."
    )


class _UiHistoryStore:
    """Thread-safe bounded JSON state store so UI history survives container restarts."""

    def __init__(self, path: Path, *, secrets: tuple[str, ...]) -> None:
        self._path = path
        self._secrets = secrets
        self._lock = threading.Lock()

    def load(self) -> list[dict[str, Any]]:
        with self._lock:
            if not self._path.is_file():
                return []
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return []
            return data if isinstance(data, list) else []

    def append(self, entry: dict[str, Any]) -> None:
        cleaned = {
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "model": str(entry.get("model", "")),
            "status_code": int(entry.get("status_code", 0)),
            "stream": bool(entry.get("stream", False)),
            "prompt_preview": redact_text(str(entry.get("prompt_preview", ""))[:120], self._secrets),
        }
        with self._lock:
            entries: list[dict[str, Any]] = []
            if self._path.is_file():
                try:
                    loaded = json.loads(self._path.read_text(encoding="utf-8"))
                    if isinstance(loaded, list):
                        entries = loaded
                except (OSError, ValueError):
                    entries = []
            entries.append(cleaned)
            entries = entries[-MAX_UI_HISTORY_ENTRIES:]
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._path.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")
            tmp_path.replace(self._path)


def create_ui_app_from_env(
    environ: Mapping[str, str] | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the minimal loopback UI gateway forwarding only UI endpoints to the API."""
    env = os.environ if environ is None else environ
    ui_token = _read_ui_token(env)
    api_url = (env.get("RCANNON_API_URL", DEFAULT_API_URL).strip() or DEFAULT_API_URL).rstrip("/")
    raw_state_path = env.get("RCANNON_UI_STATE_PATH", "").strip()
    state_path = Path(raw_state_path) if raw_state_path else DEFAULT_UI_STATE_PATH
    timeout_raw = env.get("RCANNON_UI_TIMEOUT_SECONDS", "").strip()
    timeout_seconds = float(timeout_raw) if timeout_raw else DEFAULT_UI_TIMEOUT_SECONDS
    if timeout_seconds <= 0:
        raise ValueError("RCANNON_UI_TIMEOUT_SECONDS must be greater than zero.")

    history_store = _UiHistoryStore(state_path, secrets=(ui_token,))

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        yield

    app = FastAPI(
        title="RCAnnon UI Gateway",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    def _client() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=api_url,
            timeout=timeout_seconds,
            follow_redirects=False,
            transport=transport,
        )

    def _auth_headers() -> dict[str, str]:
        return {"Authorization": f"Bearer {ui_token}"}

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "rcannon-ui-gateway"}

    @app.get("/ui/history")
    async def ui_history() -> dict[str, Any]:
        return {"entries": history_store.load()}

    @app.get("/v1/models")
    async def proxy_models() -> Response:
        try:
            async with _client() as client:
                upstream = await client.get("/v1/models", headers=_auth_headers())
        except httpx.HTTPError:
            return JSONResponse(
                status_code=503,
                content={"error": {"code": "api_unavailable", "message": "RCA API is unavailable."}},
            )
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/json"),
        )

    @app.post("/v1/chat/completions")
    async def proxy_chat_completions(request: Request) -> Response:
        try:
            payload = await request.json()
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"error": {"code": "invalid_request", "message": "Request body must be JSON."}},
            )
        if not isinstance(payload, dict):
            return JSONResponse(
                status_code=422,
                content={"error": {"code": "invalid_request", "message": "Request body must be an object."}},
            )

        stream = bool(payload.get("stream", False))
        messages = payload.get("messages")
        preview = ""
        if isinstance(messages, list) and messages:
            last = messages[-1]
            if isinstance(last, dict):
                preview = str(last.get("content", ""))

        headers = {**_auth_headers(), "Content-Type": "application/json"}
        req_id = request.headers.get("X-Request-ID")
        if req_id:
            headers["X-Request-ID"] = req_id

        try:
            async with _client() as client:
                upstream = await client.post("/v1/chat/completions", json=payload, headers=headers)
        except httpx.HTTPError:
            return JSONResponse(
                status_code=503,
                content={"error": {"code": "api_unavailable", "message": "RCA API is unavailable."}},
            )

        history_store.append(
            {
                "model": payload.get("model", ""),
                "status_code": upstream.status_code,
                "stream": stream,
                "prompt_preview": preview,
            }
        )

        content_type = upstream.headers.get("content-type", "application/json")
        resp_headers = {}
        if "x-request-id" in upstream.headers:
            resp_headers["X-Request-ID"] = upstream.headers["x-request-id"]

        if content_type.startswith("text/event-stream"):
            raw_bytes = upstream.content

            async def _iter_bytes() -> AsyncIterator[bytes]:
                yield raw_bytes

            return StreamingResponse(
                _iter_bytes(),
                status_code=upstream.status_code,
                media_type=content_type,
                headers=resp_headers,
            )

        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=content_type,
            headers=resp_headers,
        )

    return app
