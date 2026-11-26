# RCAnnon Container Image

Prebuilt multi-architecture OCI container images for RCAnnon are published to GitHub Container Registry (GHCR):

- **Registry image:** `ghcr.io/jkuzel02/rcannon`
- **Architectures:** `linux/amd64`, `linux/arm64`
- **Base OS and runtime:** `python:3.12-slim-bookworm` built from the locked dependency graph (`uv sync --frozen --no-dev --no-editable`)

## Versioning and Tag Strategy

Release versions use SemVer tags (`X.Y.Z`, starting with `0.1.0`) that match `[project].version` in `pyproject.toml` and `.release-please-manifest.json`. The GHCR workflow validates this alignment before publishing and fails closed on any mismatch.

| Tag | Source | Intended Use |
| --- | --- | --- |
| `0.1.4` (exact `X.Y.Z`) | Tagged release | Pinned reproducible release deployment | <!-- x-release-please-version -->
| `0.1` (`X.Y`) | Tagged release | Minor-line tracking |
| `latest` | Latest stable SemVer release | Tracking the current stable release (never overwritten by `main` pushes) |
| `main` | `main` branch push | Rolling development build |
| `sha-<commit>` | Every published build | Immutable commit-traceable build |

For production or benchmark reproducibility, pin deployments by immutable manifest digest (`ghcr.io/jkuzel02/rcannon@sha256:...`).

## Runtime Posture

- **Unprivileged execution:** Runs as non-root user `rcannon` (`UID=10001`, `GID=10001`).
- **Read-only filesystem compatible:** Application code and `.venv` are owned by `root:root` and read-only; writable runtime state uses `/var/lib/rcannon` (API SQLite store) and `/tmp` (UI auth/history state in Compose lives in LibreChat + MongoDB).
- **Entrypoint and port:** Starts `uvicorn --factory rca.api.server:create_app_from_env` listening on port `8000` with `SIGTERM` graceful shutdown.
- **Health probes:** Built-in `HEALTHCHECK` queries `http://127.0.0.1:8000/healthz`. Both `/healthz` and `/readyz` stay independent of target cluster and model provider outages.
- **Config verification:** Run `python -m rca.api.server --check-config` inside the container to verify UID/GID, provider mode, and optional mounted kubeconfig security without starting the HTTP server.

## Environment and Secret Configuration

Prefer Docker secret files (`*_FILE`) mounted under `/run/secrets/` rather than inline environment credentials.

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `RCANNON_PROVIDER_MODE` | No | `einfra` | `einfra` for live LLM calls or `mock` for offline smoke testing |
| `RCANNON_UI_BEARER_TOKEN_FILE` | Yes | — | Path to UI service bearer token (`RCANNON_UI_BEARER_TOKEN` alternative) |
| `RCANNON_OPERATOR_BEARER_TOKEN_FILE` | Yes | — | Path to distinct operator bearer token (`RCANNON_OPERATOR_BEARER_TOKEN` alternative) |
| `RCANNON_ALLOWED_SCOPES` | No | `{}` | JSON map of allowlisted cluster aliases to allowed namespaces |
| `RCANNON_DB_PATH` | No | `rcannon_api.db` | SQLite database path (set `/var/lib/rcannon/rcannon.db` in containers) |
| `LLM_AI_EINFRA_API_KEY_FILE` | When `einfra` | — | Path to e-INFRA CZ API key (`LLM_AI_EINFRA_API_KEY` alternative) |
| `RCANNON_KUBECONFIG_PATH` | No | — | Read-only kubeconfig mount path (for example `/etc/rcannon/kubeconfig/config`) |
| `RCANNON_KUBE_CONTEXT` | With kubeconfig | — | Named kubeconfig context (`exec`, `auth-provider`, and `insecure-skip-tls-verify` are rejected) |
| `RCANNON_TARGET_ALIAS` | No | `lab` | Target alias matching `RCANNON_ALLOWED_SCOPES` |

## Usage Examples

### With Docker Compose

Set `RCANNON_IMAGE` and start without building locally, then seed the local LibreChat administrator account:

```sh
./infrastructure/compose/scripts/init-dev-secrets.sh
RCANNON_IMAGE=ghcr.io/jkuzel02/rcannon:latest docker compose up -d --no-build --wait
docker compose --profile bootstrap run --rm ui-bootstrap
```

See [infrastructure/compose/README.md](../infrastructure/compose/README.md) for live e-INFRA CZ and read-only kubeconfig overlays.

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
- Provide `RCANNON_UI_BEARER_TOKEN_FILE`, `RCANNON_OPERATOR_BEARER_TOKEN_FILE`, `LLM_AI_EINFRA_API_KEY_FILE`, and read-only target cluster kubeconfigs (`RCANNON_KUBECONFIG_PATH`) via mounted Kubernetes `Secret` volumes (`readOnly: true`).
- Deploy LibreChat (`v0.8.9`) and MongoDB (`8.0`) on cluster-internal Services in that management cluster, pointing `librechat.yaml` at the internal `api` Service URL (`http://<rcannon-api-service>:8000/v1`) and disabling self-registration.

## Supply-Chain Provenance Verification

Published GHCR releases include SBOMs and SLSA build provenance attestations generated by GitHub Actions:

<!-- x-release-please-start-version -->
```sh
gh attestation verify oci://ghcr.io/jkuzel02/rcannon:0.1.4 --owner jkuzel02
```
<!-- x-release-please-end -->

If the GHCR package visibility is set to private, authenticate first with `docker login ghcr.io` using a token with `read:packages`.
