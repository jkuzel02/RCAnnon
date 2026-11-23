# syntax=docker/dockerfile:1

FROM python:3.12-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.7.12 /uv /uvx /usr/local/bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock .python-version ./
COPY rca ./rca
COPY prompts ./prompts

RUN uv sync --frozen --no-dev --no-editable


FROM python:3.12-slim-bookworm AS runtime

RUN groupadd --gid 10001 rcannon \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/rcannon --shell /usr/sbin/nologin rcannon \
    && mkdir -p /var/lib/rcannon /var/lib/rcannon-ui \
    && chown 10001:10001 /var/lib/rcannon /var/lib/rcannon-ui \
    && chmod 0750 /var/lib/rcannon /var/lib/rcannon-ui

WORKDIR /app

COPY --from=builder --chown=root:root /app/.venv /app/.venv
COPY --from=builder --chown=root:root /app/rca /app/rca
COPY --from=builder --chown=root:root /app/prompts /app/prompts

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER 10001:10001

EXPOSE 8000

STOPSIGNAL SIGTERM

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]

ENTRYPOINT ["uvicorn"]
CMD ["--factory", "rca.api.server:create_app_from_env", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "15"]
