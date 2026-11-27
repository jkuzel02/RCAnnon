# Independent Docker Compose Deployment

This directory packages RCAnnon's **reference Docker Compose deployment** for a workstation or standalone management host outside the target Kubernetes cluster and outside the target monitoring stack in `infrastructure/k8s/`. (Prefer running inside a **separate management Kubernetes cluster** instead? Use the published multi-arch OCI container image and Kubernetes guidance in [docs/container-image.md](../../docs/container-image.md)—RCAnnon's goal is isolation from the *target* cluster's failure domain, not tying you exclusively to Compose.)

## Services at a Glance

| Service | Image & Posture | Networks | Exposure & Role |
| --- | --- | --- | --- |
| `ui` | LibreChat `v0.8.9` (`UID=1000`, read-only rootfs, digest-pinned) | `ui_edge`, `ui_api`, `ui_db` | Publishes `127.0.0.1:3080` only; local email auth + chat UI |
| `api` | RCAnnon API (`UID=10001`, read-only rootfs) | `ui_api`, `api_egress`, `telemetry_net` | No host ports; FastAPI OpenAI facade, read-only K8s collector, LangGraph workflows |
| `mongodb` | MongoDB `8.0` (digest-pinned) | `ui_db` (`internal: true`) | No host ports; persists users & chat history in `rcannon_mongo_data` |
| `secrets-init` | RCAnnon image (one-shot, `read_only: true`, `restart: "no"`) | `network_mode: none` | Populates isolated `/run/rcannon-secrets` volumes for `api`, `ui`, and `ui-bootstrap` |
| `ui-bootstrap` | LibreChat `v0.8.9` (one-shot, `UID=1000`, `restart: "no"`) | `ui_db` (`internal: true`) | Idempotent admin seeder reading `librechat_admin_password` over `stdin` |

*Intentionally omitted upstream services:* `meilisearch` (`SEARCH=false`), `vectordb` / PostgreSQL, `rag_api`, `redis`, and external admin panels are omitted and unnecessary for login, streaming, or persisted history.

## Architecture, Operational Dependencies, and Isolation

- **Minimal viable service topology:**
  - `api`: Non-root FastAPI + LangGraph RCA runtime (`Dockerfile`, `uv sync --frozen --no-dev --no-editable`, Python `3.12`, `UID=10001`, read-only rootfs, `cap_drop: [ALL]`, `no-new-privileges:true`).
  - `ui`: MIT-licensed LibreChat pinned to `v0.8.9` by multi-arch OCI digest (`registry.librechat.ai/librechat-ai/librechat:v0.8.9@sha256:4a3967a47031c1fdba8302a6a341be7671101e40b617f7934ba25aac69fcea2b`), running as non-root `node` (`UID=1000`), read-only rootfs, `cap_drop: [ALL]`, `no-new-privileges:true`.
  - `mongodb`: MongoDB pinned to `8.0` by multi-arch OCI digest (`mongo:8.0@sha256:479e85ced54ffb8a40dade1dcde2da2a9ae858515b9415e9cc54bfb8f0575d01`) persisting LibreChat user accounts and conversation history in `rcannon_mongo_data`.
  - `secrets-init`: One-shot setup container (`network_mode: none`) generating isolated per-service runtime secrets (`0400`) inside separate named volumes (`rcannon_api_secrets`, `rcannon_ui_secrets`, `rcannon_bootstrap_secrets`), optionally seeded from host `infrastructure/compose/secrets/*.secret` files.
  - `ui-bootstrap`: Automatic one-shot container using the pinned LibreChat `v0.8.9` image to seed the initial local `ADMIN` account idempotently on a fresh MongoDB.
- **Intentionally omitted upstream services:** `meilisearch` (`SEARCH=false`), `vectordb` / PostgreSQL, `rag_api` (file search/uploads disabled in `librechat.yaml`), `redis`, and any external admin panel are omitted and are not required for login, chat streaming, or conversation persistence.
- **Network separation (`compose.yaml`):**
  - `ui_edge`: publishes only `127.0.0.1:3080` for local loopback UI access (`DOMAIN_CLIENT` and `DOMAIN_SERVER` on loopback; place behind an independent TLS reverse proxy for remote access).
  - `ui_api` (`internal: true`): allows `ui` to reach `api` on port `8000` without exposing `api` on the host.
  - `ui_db` (`internal: true`): connects `ui` and `ui-bootstrap` exclusively to `mongodb` with zero host ports.
  - `api_egress`: provides `api` outbound access to the target Kubernetes API and external LLM provider.
  - `telemetry_net` (`internal: true`): reserved for internal telemetry collection.
- **Credential separation:**
  - `ui` receives only `rcannon_ui_bearer_token` and LibreChat auth/encryption secrets (`librechat_jwt_secret`, `librechat_jwt_refresh_secret`, `librechat_creds_key`, `librechat_creds_iv`) via `/run/rcannon-secrets/*` (read in `scripts/librechat-entrypoint.sh`).
  - `api` receives `rcannon_ui_bearer_token`, `rcannon_operator_bearer_token`, and optional `OPENAI_API_KEY` (or `rcannon_llm_api_key` via `compose.openai.yaml`) plus read-only kubeconfig mounts.
  - `librechat_admin_password` is mounted only into the one-shot `ui-bootstrap` container.
- **UI hardening (`infrastructure/compose/librechat.yaml`):**
  - Exposes a single server-side custom OpenAI endpoint (`RCAnnon` -> `http://api:8000/v1`, `allowedAddresses: ["api:8000"]`) with the three workflow aliases (`rcannon-base`, `rcannon-optimal`, `rcannon-mas`) and `fetch: false`.
  - Enables local email login (`ALLOW_EMAIL_LOGIN=true`) while disabling self-signup (`ALLOW_REGISTRATION=false`), social login/registration, and unverified login.
  - Disables secondary providers, parameters/presets/prompts editing, native tools, agents, web search, file uploads/search, and hidden auto-title LLM calls (`titleConvo: false`), and strips incompatible OpenAI parameters via `dropParams`.
- **Independent health checks:** `api` (`GET /healthz`, `GET /readyz`), `mongodb` (`mongosh db.adminCommand({ ping: 1 })`), and `ui` (`GET /health`) verify service health without depending on the target Kubernetes cluster or external LLM provider.

## Quick Start (Zero-Touch `.env` or Offline Mock Mode)

1. Copy `.env.example` to `.env` and optionally paste `OPENAI_API_KEY` (leave it empty to run in offline mock mode):

```sh
cp .env.example .env
```

2. Validate and start the stack (`secrets-init` and `ui-bootstrap` run automatically and idempotently):

```sh
docker compose config --quiet
docker compose up -d --build --wait
```

3. Open `http://127.0.0.1:3080` in a browser and sign in with `admin@rcannon.local` and `rcannon-local-admin` (or your `LIBRECHAT_ADMIN_EMAIL` / `LIBRECHAT_ADMIN_PASSWORD` in `.env`). Select `rcannon-base`, `rcannon-optimal`, or `rcannon-mas` and submit an `/rca` prompt (for example `/rca context=lab namespace=demo -- checkout pods failing`).

4. Optionally verify configuration inside the `api` container or pre-generate host `.secret` files explicitly with `./infrastructure/compose/scripts/init-dev-secrets.sh`:

```sh
docker compose exec api python -m rca.api.server --check-config
```

## Administrator Bootstrap, Accounts, and Recovery

- **First-user admin behavior and idempotence:** LibreChat `v0.8.9` (`npm run create-user` -> `AuthService.registerUser`) automatically promotes the first user in an empty `users` collection to role `ADMIN` and assigns subsequent accounts role `USER`. `scripts/bootstrap-librechat-admin.sh` checks MongoDB first, pipes `librechat_admin_password` strictly over `stdin` to `npm run create-user` when `totalUsers === 0`, and re-verifies the stored `ADMIN` record. Re-running `ui-bootstrap` when the configured verified `ADMIN` already exists exits `0` without resetting passwords, reopening registration, or creating duplicate users.
- **Fail-closed conflict checks:** Bootstrap exits non-zero without altering data if credentials are missing, short (< 12 characters), multiline, or if MongoDB already contains users that conflict with the configured `LIBRECHAT_ADMIN_EMAIL` / `LIBRECHAT_ADMIN_USERNAME` / `ADMIN` role.
- **Creating additional non-privileged accounts:** With public registration closed, an operator adds non-admin `USER` accounts via LibreChat's stock CLI inside the running UI container (follow interactive password prompt or pipe via `stdin`, never pass passwords in command-line arguments):

```sh
docker compose exec -it ui npm run create-user -- operator@rcannon.local "Operator User" operator --email-verified=true
```

- **Upstream interactive recovery commands:** When resolving bootstrap conflicts or rotating local passwords:

```sh
docker compose exec -it ui npm run list-users
docker compose exec -it ui npm run reset-password
docker compose exec -it ui npm run delete-user
docker compose exec -it ui npm run create-user
```

## Using Prebuilt GHCR Images

Instead of building locally, you can deploy the multi-architecture (`linux/amd64`, `linux/arm64`) API image published to GitHub Container Registry alongside the pinned LibreChat and MongoDB digests:

```sh
RCANNON_IMAGE=ghcr.io/jkuzel02/rcannon:latest docker compose up -d --no-build --wait
```

Pin to an exact version tag (such as `0.1.7`), `:main` for rolling `main` builds, or an immutable manifest digest (`@sha256:...`). See [docs/container-image.md](../../docs/container-image.md) for the full container runtime contract, environment variables, and provenance verification. <!-- x-release-please-version -->

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

- **Local Kind (`kind-local`) automatic wiring:** Running `bash infrastructure/k8s/bootstrap.sh` generates a single-context, read-only kubeconfig at `infrastructure/compose/kubeconfig/config` using Kind's internal address (`https://local-control-plane:6443` + CA cert) and writes gitignored `compose.override.yaml` to attach `api` to Docker's external `kind` network. This avoids `127.0.0.1` (`api_unreachable`) errors on Linux, macOS, and WSL2 while preserving TLS verification.
- **Manual host-bound Kind kubeconfigs (`127.0.0.1:<port>`):** If you manually copy `~/.kube/config`, `127.0.0.1` inside the `api` container resolves to the container itself (`api_unreachable`). Re-run `bootstrap.sh`, or point a dedicated read-only kubeconfig to `https://host.docker.internal:<port>` when the cluster API certificate includes `host.docker.internal` in its SANs. Do not set `insecure-skip-tls-verify: true`.
- **VPN, DNS, and TLS:** When targeting a remote cluster over a corporate or campus VPN, verify container DNS resolves internal hostnames via Docker daemon DNS settings and mount the issuing enterprise CA certificate inside the kubeconfig bundle.
- **Target monitoring separation:** RCAnnon's runtime Compose stack must never rely on Prometheus, Grafana, or ingress running inside the target cluster.

## Security Verification, Restart, and Backup

- **Inspect container posture:** Confirm non-root users (`10001:10001` for `api`, `1000:1000` for `ui`), `Privileged: false`, no Docker socket bind mount, and only `127.0.0.1:3080` published:

```sh
docker inspect rcannon-api rcannon-ui rcannon-mongodb --format '{{.Name}} user={{.Config.User}} privileged={{.HostConfig.Privileged}} ro={{.HostConfig.ReadonlyRootfs}} ports={{.NetworkSettings.Ports}}'
```

- **Graceful restart:** `docker compose restart` preserves SQLite runs/snapshots on `rcannon_api_data` and LibreChat users/chat history on `rcannon_mongo_data`.
- **Volume backup:** Back up named volumes cleanly while stopped or via read-only volume snapshot:

```sh
docker run --rm -v rcannon_api_data:/data:ro -v "$(pwd)":/backup alpine:3.21 tar czf /backup/rcannon-api-data.tgz -C /data .
docker run --rm -v rcannon_mongo_data:/data:ro -v "$(pwd)":/backup alpine:3.21 tar czf /backup/rcannon-mongo-data.tgz -C /data .
```
