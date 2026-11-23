"""Local mock HTTP tests for the shared e-INFRA provider adapter."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from rca.contracts.provider import (
    PromptNotAllowedError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
)
from rca.providers import EInfraProvider, ProviderConfig, make_sync_complete

SYNTHETIC_SECRET = "sk-synthetic-secret-do-not-leak-987654321"


class _MockServer:
    """Thread-safe local HTTP server mocking /v1/chat/completions."""

    def __init__(self, responder) -> None:
        self.responder = responder
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                body = json.loads(raw.decode("utf-8")) if raw else {}
                with parent._lock:
                    call_index = len(parent.requests)
                    parent.requests.append(
                        {
                            "path": self.path,
                            "auth": self.headers.get("Authorization"),
                            "body": body,
                        }
                    )
                status, payload, delay = parent.responder(call_index, body)
                if delay > 0:
                    time.sleep(delay)
                if isinstance(payload, bytes):
                    encoded = payload
                elif isinstance(payload, str):
                    encoded = payload.encode("utf-8")
                else:
                    encoded = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                try:
                    self.wfile.write(encoded)
                except BrokenPipeError:
                    pass

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                del format, args

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        host, port = self._server.server_address
        self.base_url = f"http://{host}:{port}/v1"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> _MockServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


def _ok_completion(
    content: str = "Root cause identified.",
    *,
    usage: dict[str, int] | None = None,
    finish_reason: str = "stop",
    request_id: str = "chatcmpl-test-123",
    model: str = "deepseek-v4.1-flash",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": request_id,
        "object": "chat.completion",
        "created": 1700000000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
    }
    if usage is not None:
        body["usage"] = usage
    return body


def test_config_from_env_aliases_secret_file_and_redaction(tmp_path: Path) -> None:
    secret_file = tmp_path / "api.key"
    secret_file.write_text(f"{SYNTHETIC_SECRET}\n", encoding="utf-8")

    cfg = ProviderConfig.from_env(
        {
            "LLM_AI_EINFRA_API_KEY_FILE": str(secret_file),
            "LLM_AI_EINFRA_MODEL": "qwen3.8-27b",
            "LLM_AI_EINFRA_MAX_RETRIES": "2",
            "LLM_AI_EINFRA_MAX_OUTPUT_TOKENS": "1024",
            "LLM_AI_EINFRA_MAX_CONTEXT_CHARS": "20000",
        }
    )
    assert cfg.api_key == SYNTHETIC_SECRET
    assert cfg.model == "qwen3.8-27b"
    assert cfg.max_retries == 2
    assert cfg.max_output_tokens == 1024
    assert cfg.max_context_chars == 20000
    assert SYNTHETIC_SECRET not in repr(cfg)

    alias_cfg = ProviderConfig.from_env({"EINFRA_AI_TOKEN": SYNTHETIC_SECRET})
    assert alias_cfg.api_key == SYNTHETIC_SECRET
    assert alias_cfg.max_retries == 0


def test_successful_completion_with_usage_and_sync_adapter() -> None:
    def respond(_idx: int, _body: dict[str, Any]):
        return (
            200,
            _ok_completion(
                "Memory limit 64Mi is below the 128Mi working set.",
                usage={"prompt_tokens": 110, "completion_tokens": 25, "total_tokens": 135},
            ),
            0.0,
        )

    with _MockServer(respond) as server:
        provider = EInfraProvider(
            ProviderConfig(api_key=SYNTHETIC_SECRET, base_url=server.base_url)
        )
        response = provider.complete_response("base.md", {"context": {"incident": "OOM"}})
        assert response.text == "Memory limit 64Mi is below the 128Mi working set."
        assert response.model == "deepseek-v4.1-flash"
        assert response.request_id is not None
        assert response.attempts == 1
        assert response.duration_seconds >= 0.0
        assert response.input_tokens == 110
        assert response.output_tokens == 25
        assert response.total_tokens == 135
        assert response.finish_status == "stop"

        complete = make_sync_complete(provider)
        assert complete("base.md", {"context": {"incident": "OOM"}}) == response.text
        assert server.requests[0]["path"] == "/v1/chat/completions"
        assert server.requests[0]["auth"] == f"Bearer {SYNTHETIC_SECRET}"


def test_nullable_token_totals_are_never_invented() -> None:
    def respond(_idx: int, _body: dict[str, Any]):
        return 200, _ok_completion("Diagnosis without usage block.", usage=None), 0.0

    with _MockServer(respond) as server:
        provider = EInfraProvider(
            ProviderConfig(api_key=SYNTHETIC_SECRET, base_url=server.base_url)
        )
        response = provider.complete_response("base.md", {"context": {}})
        assert response.text == "Diagnosis without usage block."
        assert response.input_tokens is None
        assert response.output_tokens is None
        assert response.total_tokens is None


@pytest.mark.parametrize("status_code", [401, 403])
def test_auth_failure_does_not_retry_and_redacts_secret(status_code: int) -> None:
    def respond(_idx: int, _body: dict[str, Any]):
        return (
            status_code,
            {"error": {"message": f"Invalid token {SYNTHETIC_SECRET}", "type": "auth_error"}},
            0.0,
        )

    with _MockServer(respond) as server:
        provider = EInfraProvider(
            ProviderConfig(
                api_key=SYNTHETIC_SECRET,
                base_url=server.base_url,
                max_retries=2,
                retry_backoff_seconds=0.0,
            )
        )
        with pytest.raises(ProviderAuthError) as exc_info:
            provider.complete("base.md", {"context": {}})

        assert len(server.requests) == 1
        assert SYNTHETIC_SECRET not in str(exc_info.value)


def test_throttling_respects_default_zero_retries_and_succeeds_when_enabled() -> None:
    def always_429(_idx: int, _body: dict[str, Any]):
        return 429, {"error": {"message": "Rate limit exceeded"}}, 0.0

    with _MockServer(always_429) as server:
        provider = EInfraProvider(
            ProviderConfig(api_key=SYNTHETIC_SECRET, base_url=server.base_url)
        )
        with pytest.raises(ProviderRateLimitError):
            provider.complete("base.md", {"context": {}})
        assert len(server.requests) == 1

    def flaky_429(idx: int, _body: dict[str, Any]):
        if idx == 0:
            return 429, {"error": {"message": "Rate limit exceeded"}}, 0.0
        return 200, _ok_completion("Recovered after retry."), 0.0

    with _MockServer(flaky_429) as server:
        provider = EInfraProvider(
            ProviderConfig(
                api_key=SYNTHETIC_SECRET,
                base_url=server.base_url,
                max_retries=1,
                retry_backoff_seconds=0.0,
            )
        )
        response = provider.complete_response("base.md", {"context": {}})
        assert response.text == "Recovered after retry."
        assert response.attempts == 2
        assert len(server.requests) == 2


def test_server_failure_respects_retry_cap() -> None:
    def always_503(_idx: int, _body: dict[str, Any]):
        return 503, {"error": {"message": "Service unavailable"}}, 0.0

    with _MockServer(always_503) as server:
        provider = EInfraProvider(
            ProviderConfig(
                api_key=SYNTHETIC_SECRET,
                base_url=server.base_url,
                max_retries=2,
                retry_backoff_seconds=0.0,
            )
        )
        with pytest.raises(ProviderError):
            provider.complete("base.md", {"context": {}})
        assert len(server.requests) == 3


def test_timeout_and_deadline_enforced() -> None:
    def slow(_idx: int, _body: dict[str, Any]):
        return 200, _ok_completion("Too late."), 0.5

    with _MockServer(slow) as server:
        provider = EInfraProvider(
            ProviderConfig(
                api_key=SYNTHETIC_SECRET,
                base_url=server.base_url,
                timeout_seconds=0.1,
                deadline_seconds=0.15,
                max_retries=3,
                retry_backoff_seconds=0.0,
            )
        )
        with pytest.raises(ProviderTimeoutError):
            provider.complete("base.md", {"context": {}})
        assert len(server.requests) <= 2


@pytest.mark.parametrize(
    "payload",
    [
        b"not valid json",
        _ok_completion("   "),
        _ok_completion("Truncated answer", finish_reason="length"),
    ],
)
def test_malformed_empty_or_truncated_output_raises_without_retry(payload: Any) -> None:
    def respond(_idx: int, _body: dict[str, Any]):
        return 200, payload, 0.0

    with _MockServer(respond) as server:
        provider = EInfraProvider(
            ProviderConfig(
                api_key=SYNTHETIC_SECRET,
                base_url=server.base_url,
                max_retries=2,
                retry_backoff_seconds=0.0,
            )
        )
        with pytest.raises(ProviderResponseError):
            provider.complete("base.md", {"context": {}})
        assert len(server.requests) == 1


def test_prompt_allowlist_and_context_limit() -> None:
    provider = EInfraProvider(
        ProviderConfig(
            api_key=SYNTHETIC_SECRET,
            base_url="http://127.0.0.1:9/v1",
            max_context_chars=50,
        )
    )
    with pytest.raises(PromptNotAllowedError):
        provider.complete("../README.md", {"context": {}})
    with pytest.raises(PromptNotAllowedError):
        provider.complete("unknown.md", {"context": {}})
    with pytest.raises(ValueError, match="max_context_chars"):
        provider.complete("base.md", {"context": {"incident": "x" * 100}})


def test_provider_is_thread_safe_for_parallel_calls() -> None:
    def respond(_idx: int, body: dict[str, Any]):
        user_payload = json.loads(body["messages"][1]["content"])
        return 200, _ok_completion(f"ok-{user_payload['n']}"), 0.0

    with _MockServer(respond) as server:
        complete = make_sync_complete(
            EInfraProvider(ProviderConfig(api_key=SYNTHETIC_SECRET, base_url=server.base_url))
        )
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda n: complete("base.md", {"n": n}), range(8)))
        assert results == [f"ok-{n}" for n in range(8)]
