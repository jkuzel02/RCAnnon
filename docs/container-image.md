# RCAnnon Container Image

## Run the API Container

The RCAnnon image contains the FastAPI facade, Kubernetes evidence collector, and LangGraph workflows. Set two distinct bearer tokens in your shell (`RCANNON_UI_BEARER_TOKEN` for `/v1/*` chat clients and `RCANNON_OPERATOR_BEARER_TOKEN` for `/api/v1/*` operator routes) and start the container:

```sh
docker run -d --name rcannon \
  -p 127.0.0.1:8000:8000 \
  --mount type=volume,source=rcannon-api-data,target=/var/lib/rcannon \
  -e RCANNON_DB_PATH=/var/lib/rcannon/rcannon.db \
  -e RCANNON_UI_BEARER_TOKEN -e RCANNON_OPERATOR_BEARER_TOKEN \
  -e RCANNON_PROVIDER_MODE=mock \
  ghcr.io/jkuzel02/rcannon:latest
```

Query `/v1/chat/completions` with `curl` or point any OpenAI-compatible client at `http://127.0.0.1:8000/v1` using `$RCANNON_UI_BEARER_TOKEN`.

This starts the service in mock mode. To analyze your own cluster, supply the
provider key and read-only kubeconfig listed under
[Environment and secret configuration](#environment-and-secret-configuration).
To try a report with a bundled incident first, use the [getting-started demo](getting-started.md).

Prebuilt multi-architecture OCI container images for RCAnnon are published to GitHub Container Registry (GHCR):

- **Registry image:** `ghcr.io/jkuzel02/rcannon`
- **Architectures:** `linux/amd64`, `linux/arm64`
- **Base OS and runtime:** `python:3.12-slim-bookworm` built from the locked dependency graph (`uv sync --frozen --no-dev --no-editable`)

## Versioning and Tag Strategy

Release versions use SemVer tags (`X.Y.Z`, starting with `0.1.0`) that match `[project].version` in `pyproject.toml` and `.release-please-manifest.json`. The GHCR workflow validates this alignment before publishing and fails closed on any mismatch.

| Tag | Source | Intended Use |
| --- | --- | --- |
| `0.3.0` (exact `X.Y.Z`) | Tagged release | Pinned reproducible release deployment | <!-- x-release-please-version -->
| `0.1` (`X.Y`) | Tagged release | Minor-line tracking |
| `latest` | Latest stable SemVer release | Tracking the current stable release (never overwritten by `main` pushes) |
| `main` | `main` branch push | Rolling development build |
| `sha-<commit>` | Every published build | Immutable commit-traceable build |

For production or benchmark reproducibility, pin deployments by immutable manifest digest (`ghcr.io/jkuzel02/rcannon@sha256:...`).

## Runtime Posture

- **Unprivileged execution:** Runs as non-root user `rcannon` (`UID=10001`, `GID=10001`).
- **Read-only filesystem compatible:** Application code and `.venv` are owned by `root:root` and read-only; writable runtime state uses `/var/lib/rcannon` (API SQLite store) and `/tmp`.
- **Entrypoint and port:** Starts `uvicorn --factory rca.api.server:create_app_from_env` listening on port `8000` with `SIGTERM` graceful shutdown.
- **Health probes:** Built-in `HEALTHCHECK` queries `http://127.0.0.1:8000/healthz`. Both `/healthz` and `/readyz` stay independent of target cluster and model provider outages.
- **Config verification:** Run `python -m rca.api.server --check-config` inside the container to verify UID/GID, provider mode, and optional mounted kubeconfig security without starting the HTTP server.

## Environment and Secret Configuration

Prefer Docker secret files (`*_FILE`) mounted under `/run/secrets/` rather than inline environment credentials.

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `RCANNON_PROVIDER_MODE` | No | `openai` | `auto` (uses `openai` when an API key is set, else `mock`), `openai`, or `mock` |
| `RCANNON_UI_BEARER_TOKEN_FILE` | Yes | — | Path to UI service bearer token (`RCANNON_UI_BEARER_TOKEN` alternative) |
| `RCANNON_OPERATOR_BEARER_TOKEN_FILE` | Yes | — | Path to distinct operator bearer token (`RCANNON_OPERATOR_BEARER_TOKEN` alternative) |
| `RCANNON_ALLOWED_SCOPES` | No | `{}` | JSON map of allowlisted cluster aliases to allowed namespaces |
| `RCANNON_DB_PATH` | No | `rcannon_api.db` | SQLite database path (set `/var/lib/rcannon/rcannon.db` in containers) |
| `OPENAI_API_KEY_FILE` | When `openai` | — | Path to OpenAI-compatible API key (`OPENAI_API_KEY` alternative) |
| `OPENAI_BASE_URL` | No | `https://api.openai.com/v1` | Base URL for OpenAI or any OpenAI-compatible Chat Completions endpoint |
| `OPENAI_MODEL` | No | `gpt-4o-mini` | Model ID passed to `/v1/chat/completions` |
| `RCANNON_KUBECONFIG_PATH` | No | — | Read-only kubeconfig mount path (for example `/etc/rcannon/kubeconfig/config`) |
| `RCANNON_KUBE_CONTEXT` | With kubeconfig | — | Named kubeconfig context (`exec`, `auth-provider`, and `insecure-skip-tls-verify` are rejected) |
| `RCANNON_TARGET_ALIAS` | No | `lab` | Target alias matching `RCANNON_ALLOWED_SCOPES` |

## Usage Examples

### With Docker Compose

Follow [Getting Started](getting-started.md) to run the published image in demo mode, or [Deploy with Docker Compose](deploy-compose.md) for live inference and target cluster configuration. See [Compose operations](../infrastructure/compose/README.md) for advanced overrides.

### Hardened Standalone Container (`mock` mode)

```sh
docker run --rm \
  --user 10001:10001 \
  --read-only \
  --cap-drop=ALL \
  --security-opt=no-new-privileges:true \
  --tmpfs /tmp:size=32m,noexec,nosuid,nodev \
  -p 127.0.0.1:8000:8000 \
  -e RCANNON_PROVIDER_MODE=mock \
  -e RCANNON_DB_PATH=/tmp/rcannon.db \
  -e RCANNON_ALLOWED_SCOPES='{"lab":["demo"]}' \
  -e RCANNON_UI_BEARER_TOKEN=example-ui-token \
  -e RCANNON_OPERATOR_BEARER_TOKEN=example-operator-token \
  ghcr.io/jkuzel02/rcannon:latest
```

### In a Separate Management Kubernetes Cluster

You are not limited to Docker Compose. The published `ghcr.io/jkuzel02/rcannon` OCI image runs cleanly inside a **separate management or operations Kubernetes cluster** while keeping the runtime isolated from the failing target cluster's blast radius:

- Run the API pod with non-root security settings (`runAsUser: 10001`, `runAsGroup: 10001`, `readOnlyRootFilesystem: true`, `allowPrivilegeEscalation: false`, `capabilities: { drop: ["ALL"] }`) and a persistent volume mounted at `/var/lib/rcannon`.
- Provide `RCANNON_UI_BEARER_TOKEN_FILE`, `RCANNON_OPERATOR_BEARER_TOKEN_FILE`, `OPENAI_API_KEY_FILE`, and read-only target cluster kubeconfigs (`RCANNON_KUBECONFIG_PATH`) via mounted Kubernetes `Secret` volumes (`readOnly: true`).
- Expose the API only on loopback or behind an independently managed TLS reverse proxy / Kubernetes `Ingress` or `NetworkPolicy`, and connect any external OpenAI-compatible client to `http://<rcannon-api-service>:8000/v1`.

## Supply-Chain Provenance Verification

Published GHCR releases include SBOMs and SLSA build provenance attestations generated by GitHub Actions:

<!-- x-release-please-start-version -->
```sh
gh attestation verify oci://ghcr.io/jkuzel02/rcannon:0.3.0 --owner jkuzel02
```
<!-- x-release-please-end -->

If the GHCR package visibility is set to private, authenticate first with `docker login ghcr.io` using a token with `read:packages`.
