# Independent Docker Compose Deployment

This directory packages RCAnnon's **reference Docker Compose deployment** for a workstation or standalone management host outside the target Kubernetes cluster and outside the target monitoring stack in `infrastructure/k8s/`. (Prefer running inside a **separate management Kubernetes cluster** instead? Use the published multi-arch OCI container image and Kubernetes guidance in [docs/container-image.md](../../docs/container-image.md)—RCAnnon's goal is isolation from the *target* cluster's failure domain, not tying you exclusively to Compose.)

## Services at a Glance

| Service | Image & Posture | Networks | Exposure & Role |
| --- | --- | --- | --- |
| `rcannon` | RCAnnon API (`UID=10001`, read-only rootfs) | `api_egress`, `telemetry_net` | Publishes `127.0.0.1:8000` only; FastAPI OpenAI facade, read-only K8s collector, LangGraph workflows |
| `secrets-init` | Python 3.12 slim (one-shot) | `network_mode: none` | Generates distinct `/v1/*` chat (`rcannon_ui_bearer_token`) and `/api/v1/*` operator (`rcannon_operator_bearer_token`) tokens for `rcannon` |

## Architecture, Operational Dependencies, and Isolation

- **Minimal viable service topology:**
  - `rcannon`: Non-root FastAPI + LangGraph RCA runtime (`Dockerfile`, `uv sync --frozen --no-dev --no-editable`, Python `3.12`, `UID=10001`, read-only rootfs, `cap_drop: [ALL]`, `no-new-privileges:true`).
  - `secrets-init`: One-shot API token initializer running with `network_mode: none` and `read_only: true`.
- **Network separation (`compose.yaml`):**
  - `rcannon` publishes only `127.0.0.1:8000` for local loopback API access (place behind an independent TLS reverse proxy for remote access).
  - `api_egress`: provides `rcannon` outbound access to the target Kubernetes API and external LLM provider.
  - `telemetry_net` (`internal: true`): reserved for internal telemetry collection.
- **Credential separation:**
  - `rcannon` receives `rcannon_ui_bearer_token` (for `/v1/*` OpenAI-compatible clients), `rcannon_operator_bearer_token` (for `/api/v1/*` operator routes), and optional `OPENAI_API_KEY` (or `rcannon_llm_api_key` via `compose.openai.yaml`) plus read-only kubeconfig mounts.
- **Independent health checks:** `rcannon` (`GET /healthz`, `GET /readyz`) verifies service health without depending on the target Kubernetes cluster or external LLM provider.

## Quick Start

Use [Getting Started](../../docs/getting-started.md) for an offline demo with a bundled incident and `curl` examples. Use [Deploy RCAnnon with Docker Compose](../../docs/deploy-compose.md) to connect a real cluster and model provider.

Run `docker compose up -d --wait` for the API. If you use a generated Kind override, rerun the lab bootstrap to refresh it. To verify the API container's configuration after startup:

```sh
docker compose exec rcannon python -m rca.api.server --check-config
```

## Using Prebuilt GHCR Images

Docker Compose uses `ghcr.io/jkuzel02/rcannon:latest`. See [Container Image](../../docs/container-image.md) for pinned deployments, supported architectures, and provenance.

For a local source build, use the [contributor commands](../../CONTRIBUTING.md#3-minute-setup--offline-test-loop).

## Enabling Live OpenAI-Compatible API Inference

1. Create `infrastructure/compose/secrets/llm_api_key.secret` containing an API key for OpenAI or any OpenAI-compatible Chat Completions endpoint (no trailing quotes). Startup fails closed if `RCANNON_PROVIDER_MODE=openai` is active and the secret file is missing or empty.
2. Optionally set `OPENAI_BASE_URL` (default `https://api.openai.com/v1`) and `OPENAI_MODEL` (default `gpt-4o-mini`) in `.env` or your shell.
3. Start with the provider overlay:

```sh
docker compose -f compose.yaml -f infrastructure/compose/compose.openai.yaml up -d --wait
```

## Mounting Read-Only Target Kubeconfig and CA Files

1. Place a dedicated read-only kubeconfig and any referenced CA/client certificates in a host directory (default `infrastructure/compose/kubeconfig/`, which is gitignored).
2. Keep certificate paths relative to the kubeconfig file (for example `certificate-authority: ca.crt`) or inline `certificate-authority-data` so the mount remains portable inside `/etc/rcannon/kubeconfig/`.
3. Ensure files are readable by container UID `10001` (`chmod 0755` on the directory and `chmod 0644` on files, or `chown :10001` + `chmod 0750`/`0640`).
4. Start with the Kubernetes overlay:

```sh
docker compose -f compose.yaml -f infrastructure/compose/compose.kube.yaml up -d --wait
```

Startup rejects kubeconfigs with `exec` plugins, `auth-provider` blocks, or `insecure-skip-tls-verify: true`.

## Linux and macOS Reachability to Kind and Remote Clusters

- **Local Kind (`kind-local`) automatic wiring:** Running `bash infrastructure/k8s/bootstrap.sh` generates a single-context, read-only kubeconfig at `infrastructure/compose/kubeconfig/config` using Kind's internal address (`https://local-control-plane:6443` + CA cert) and writes gitignored `compose.override.yaml` to attach `rcannon` to Docker's external `kind` network. This avoids `127.0.0.1` (`api_unreachable`) errors on Linux, macOS, and WSL2 while preserving TLS verification.
- **Manual host-bound Kind kubeconfigs (`127.0.0.1:<port>`):** If you manually copy `~/.kube/config`, `127.0.0.1` inside the `rcannon` container resolves to the container itself (`api_unreachable`). Re-run `bootstrap.sh`, or point a dedicated read-only kubeconfig to `https://host.docker.internal:<port>` when the cluster API certificate includes `host.docker.internal` in its SANs. Do not set `insecure-skip-tls-verify: true`.
- **VPN, DNS, and TLS:** When targeting a remote cluster over a corporate or campus VPN, verify container DNS resolves internal hostnames via Docker daemon DNS settings and mount the issuing enterprise CA certificate inside the kubeconfig bundle.
- **Target monitoring separation:** RCAnnon's runtime Compose stack must never rely on Prometheus, Grafana, or ingress running inside the target cluster.

## Security Verification, Restart, and Backup

- **Inspect container posture:** Confirm non-root user (`10001:10001` for `rcannon`), `Privileged: false`, no Docker socket bind mount, and only `127.0.0.1:8000` published:

```sh
docker inspect rcannon --format '{{.Name}} user={{.Config.User}} privileged={{.HostConfig.Privileged}} ro={{.HostConfig.ReadonlyRootfs}} ports={{.NetworkSettings.Ports}}'
```

- **Graceful restart:** `docker compose restart` preserves SQLite runs/snapshots on `rcannon_api_data`.
- **Volume backup:** Back up the named volume cleanly while stopped or via read-only volume snapshot:

```sh
docker run --rm -v rcannon_api_data:/data:ro -v "$(pwd)":/backup alpine:3.21 tar czf /backup/rcannon-api-data.tgz -C /data .
```
