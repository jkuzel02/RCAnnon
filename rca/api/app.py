"""FastAPI application and OpenAI-compatible facade for RCAnnon."""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from rca.api.auth import AuthRole, require_operator_auth, require_ui_auth
from rca.api.config import ApiConfig
from rca.api.models import (
    OPENAI_MODEL_TO_VARIANT,
    ChatCompletionRequest,
    CreateRunRequest,
    CreateSnapshotRequest,
    SnapshotSummary,
    parse_chat_directive,
)
from rca.api.rendering import redact_text, render_run_markdown
from rca.api.service import (
    ApiProviderUnavailableError,
    ApiTimeoutError,
    RcaApiService,
    RunNotFoundError,
    ScopeDeniedError,
    SnapshotNotFoundError,
)
from rca.api.storage import RunStore
from rca.contracts.provider import CompleteFn
from rca.contracts.runtime import CollectorFn, RunResult, TargetSelector, VariantName
from rca.workflows import list_variants

_SAFE_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")


class _PayloadTooLargeError(Exception):
    """Internal signal raised when an incoming HTTP body exceeds max_bundle_bytes."""


class _RequestContextAndSizeMiddleware:
    """Pure ASGI middleware for X-Request-ID propagation, X-Run-ID, and body-size caps."""

    def __init__(self, app: ASGIApp, *, max_bundle_bytes: int) -> None:
        self.app = app
        self.max_bundle_bytes = max_bundle_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        incoming_id = headers.get("x-request-id", "").strip()
        request_id = (
            incoming_id
            if incoming_id and _SAFE_REQUEST_ID_RE.fullmatch(incoming_id)
            else f"req_{uuid.uuid4().hex}"
        )
        state = scope.setdefault("state", {})
        state["request_id"] = request_id

        raw_content_length = headers.get("content-length")
        if raw_content_length is not None:
            try:
                content_length = int(raw_content_length)
            except ValueError:
                response = JSONResponse(
                    status_code=400,
                    content={
                        "error": {
                            "code": "invalid_request",
                            "message": "Invalid Content-Length header.",
                        }
                    },
                    headers={"X-Request-ID": request_id},
                )
                await response(scope, receive, send)
                return
            if content_length > self.max_bundle_bytes:
                response = JSONResponse(
                    status_code=413,
                    content={
                        "error": {
                            "code": "payload_too_large",
                            "message": "Request body exceeds configured size limit.",
                        }
                    },
                    headers={"X-Request-ID": request_id},
                )
                await response(scope, receive, send)
                return

        response_started = False

        async def wrapped_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                raw_headers = list(message.get("headers", []))
                raw_headers.append((b"x-request-id", request_id.encode("latin-1")))
                run_id = state.get("run_id")
                if isinstance(run_id, str) and run_id:
                    raw_headers.append((b"x-run-id", run_id.encode("latin-1")))
                message = {**message, "headers": raw_headers}
            await send(message)

        received_bytes = 0

        async def capped_receive() -> Message:
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                chunk = message.get("body", b"")
                received_bytes += len(chunk)
                if received_bytes > self.max_bundle_bytes:
                    raise _PayloadTooLargeError()
            return message

        try:
            await self.app(scope, capped_receive, wrapped_send)
        except _PayloadTooLargeError:
            if not response_started:
                response = JSONResponse(
                    status_code=413,
                    content={
                        "error": {
                            "code": "payload_too_large",
                            "message": "Request body exceeds configured size limit.",
                        }
                    },
                    headers={"X-Request-ID": request_id},
                )
                await response(scope, receive, send)


def _sse_chunk(
    run_id: str,
    model: str,
    created: int,
    delta: dict[str, Any],
    *,
    finish_reason: str | None = None,
) -> str:
    return json.dumps(
        {
            "id": run_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": finish_reason,
                }
            ],
        },
        ensure_ascii=False,
    )


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


def create_app(
    *,
    config: ApiConfig,
    complete: CompleteFn,
    collector: CollectorFn | None = None,
    store: RunStore | None = None,
) -> FastAPI:
    """Build the FastAPI service exposing scoped OpenAI and operator RCA endpoints."""
    owned_store = store is None
    active_store = store or RunStore(config.db_path)
    service = RcaApiService(
        config=config,
        store=active_store,
        complete=complete,
        collector=collector,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        active_store.initialize()
        try:
            yield
        finally:
            service.shutdown()
            if owned_store:
                active_store.close()

    app = FastAPI(
        title="RCAnnon API",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.config = config
    app.state.store = active_store
    app.state.service = service
    app.add_middleware(
        _RequestContextAndSizeMiddleware,
        max_bundle_bytes=config.max_bundle_bytes,
    )

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        safe_details = [
            {
                "loc": list(err.get("loc", ())),
                "msg": redact_text(str(err.get("msg", "Invalid value.")), service.secrets),
                "type": str(err.get("type", "value_error")),
            }
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "invalid_request",
                    "message": "Request validation failed.",
                    "details": safe_details,
                }
            },
        )

    @app.exception_handler(ValueError)
    async def _value_error_handler(_request: Request, exc: ValueError) -> JSONResponse:
        return _error_response(
            422,
            "invalid_request",
            redact_text(str(exc), service.secrets),
        )

    @app.exception_handler(HTTPException)
    async def _http_error_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict):
            return JSONResponse(status_code=exc.status_code, content=detail)
        return _error_response(exc.status_code, "http_error", str(detail))

    @app.exception_handler(ScopeDeniedError)
    async def _scope_error_handler(_request: Request, exc: ScopeDeniedError) -> JSONResponse:
        return _error_response(403, "forbidden", str(exc))

    @app.exception_handler(SnapshotNotFoundError)
    @app.exception_handler(RunNotFoundError)
    async def _not_found_handler(_request: Request, exc: Exception) -> JSONResponse:
        return _error_response(404, "not_found", str(exc))

    @app.exception_handler(ApiProviderUnavailableError)
    async def _provider_error_handler(
        _request: Request, exc: ApiProviderUnavailableError
    ) -> JSONResponse:
        return _error_response(503, "provider_unavailable", str(exc))

    @app.exception_handler(ApiTimeoutError)
    async def _timeout_error_handler(_request: Request, exc: ApiTimeoutError) -> JSONResponse:
        return _error_response(504, "timeout", str(exc))

    @app.exception_handler(Exception)
    async def _generic_error_handler(_request: Request, _exc: Exception) -> JSONResponse:
        return _error_response(500, "internal_error", "An unexpected internal error occurred.")

    async def _run_with_disconnect(
        request: Request,
        *,
        variant: VariantName,
        snapshot_id: str | None,
        target: TargetSelector | None,
        incident: str | None,
    ) -> RunResult:
        request_id = getattr(request.state, "request_id", f"req_{uuid.uuid4().hex}")
        cancel_event = threading.Event()
        run_task = asyncio.create_task(
            service.execute_run(
                request_id=request_id,
                variant=variant,
                snapshot_id=snapshot_id,
                target=target,
                incident=incident,
                cancel_event=cancel_event,
            )
        )

        async def _watch_disconnect() -> None:
            while not run_task.done():
                if await request.is_disconnected():
                    cancel_event.set()
                    run_task.cancel()
                    return
                await asyncio.sleep(0.05)

        watcher = asyncio.create_task(_watch_disconnect())
        try:
            run = await run_task
            request.state.run_id = run.run_id
            return run
        finally:
            watcher.cancel()
            try:
                await watcher
            except asyncio.CancelledError:
                pass

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        try:
            ok = active_store.ping()
        except Exception:  # noqa: BLE001
            ok = False
        if not ok:
            return _error_response(503, "not_ready", "Run store is unavailable.")
        return JSONResponse(
            status_code=200,
            content={"status": "ready", "variants": list(list_variants())},
        )

    @app.get("/v1/models")
    async def list_openai_models(
        _role: AuthRole = Depends(require_ui_auth),
    ) -> dict[str, Any]:
        return {
            "object": "list",
            "data": [
                {
                    "id": model_id,
                    "object": "model",
                    "created": 0,
                    "owned_by": "rcannon",
                }
                for model_id in OPENAI_MODEL_TO_VARIANT
            ],
        }

    @app.post("/v1/chat/completions", response_model=None)
    async def chat_completions(
        request: Request,
        payload: ChatCompletionRequest,
        _role: AuthRole = Depends(require_ui_auth),
    ) -> JSONResponse | EventSourceResponse:
        directive = parse_chat_directive(
            payload.messages,
            max_prompt_chars=config.max_prompt_chars,
        )
        variant = OPENAI_MODEL_TO_VARIANT[payload.model]
        service.precheck_run_inputs(
            snapshot_id=directive.snapshot_id,
            target=directive.target,
        )
        run = await _run_with_disconnect(
            request,
            variant=variant,
            snapshot_id=directive.snapshot_id,
            target=directive.target,
            incident=directive.incident,
        )
        markdown = render_run_markdown(run, secrets=service.secrets)
        created = int(time.time())

        if payload.stream:
            async def _event_stream() -> AsyncIterator[dict[str, str]]:
                yield {
                    "data": _sse_chunk(
                        run.run_id,
                        payload.model,
                        created,
                        {"role": "assistant", "content": ""},
                    )
                }
                yield {
                    "data": _sse_chunk(
                        run.run_id,
                        payload.model,
                        created,
                        {"content": markdown},
                    )
                }
                yield {
                    "data": _sse_chunk(
                        run.run_id,
                        payload.model,
                        created,
                        {},
                        finish_reason="stop",
                    )
                }
                yield {"data": "[DONE]"}

            return EventSourceResponse(_event_stream())

        body: dict[str, Any] = {
            "id": run.run_id,
            "object": "chat.completion",
            "created": created,
            "model": payload.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": markdown},
                    "finish_reason": "stop",
                }
            ],
        }
        if run.usage.reported_total_tokens is not None:
            body["usage"] = {
                "prompt_tokens": run.usage.reported_input_tokens or 0,
                "completion_tokens": run.usage.reported_output_tokens or 0,
                "total_tokens": run.usage.reported_total_tokens,
            }
        return JSONResponse(status_code=200, content=body)

    @app.post("/api/v1/snapshots", status_code=201, response_model=SnapshotSummary)
    async def create_snapshot(
        payload: CreateSnapshotRequest,
        _role: AuthRole = Depends(require_operator_auth),
    ) -> SnapshotSummary:
        snapshot, expires_at = await service.create_snapshot_async(payload)
        return service.to_snapshot_summary(snapshot, expires_at)

    @app.get("/api/v1/snapshots/{snapshot_id}", response_model=SnapshotSummary)
    async def get_snapshot(
        snapshot_id: str,
        _role: AuthRole = Depends(require_operator_auth),
    ) -> SnapshotSummary:
        return service.get_snapshot_summary(snapshot_id)

    @app.post("/api/v1/runs", response_model=RunResult)
    async def create_run(
        request: Request,
        payload: CreateRunRequest,
        _role: AuthRole = Depends(require_operator_auth),
    ) -> RunResult:
        return await _run_with_disconnect(
            request,
            variant=payload.variant,
            snapshot_id=payload.snapshot_id,
            target=payload.target,
            incident=payload.incident,
        )

    @app.get("/api/v1/runs")
    async def list_runs(
        limit: int = Query(default=20, ge=1, le=100),
        _role: AuthRole = Depends(require_operator_auth),
    ) -> dict[str, Any]:
        return {"runs": [run.model_dump() for run in service.list_runs(limit=limit)]}

    @app.get("/api/v1/runs/{run_id}", response_model=RunResult)
    async def get_run(
        run_id: str,
        _role: AuthRole = Depends(require_operator_auth),
    ) -> RunResult:
        return service.get_run(run_id)

    return app
