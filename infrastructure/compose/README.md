# Independent Docker Compose Deployment

This directory packages RCAnnon's **reference Docker Compose deployment** for a workstation or standalone management host outside the target Kubernetes cluster and outside the target monitoring stack in `infrastructure/k8s/`. (Prefer running inside a **separate management Kubernetes cluster** instead? Use the published multi-arch OCI container image and Kubernetes guidance in [docs/container-image.md](../../docs/container-image.md)—RCAnnon's goal is isolation from the *target* cluster's failure domain, not tying you exclusively to Compose.)

## Services at a Glance

| Service | Image & Posture | Networks | Exposure & Role |
| --- | --- | --- | --- |
| `ui` | LibreChat `v0.8.9` (`UID=1000`, read-only rootfs, digest-pinned) | `ui_edge`, `ui_api`, `ui_db` | Publishes `127.0.0.1:3080` only; local email auth + chat UI |
| `api` | RCAnnon API (`UID=10001`, read-only rootfs) | `ui_api`, `api_egress`, `telemetry_net` | No host ports; FastAPI OpenAI facade, read-only K8s collector, LangGraph workflows |
| `mongodb` | MongoDB `8.0` (digest-pinned) | `ui_db` (`internal: true`) | No host ports; persists users & chat history in `rcannon_mongo_data` |
| `ui-bootstrap` | LibreChat `v0.8.9` (`profiles: ["bootstrap"]`, `UID=1000`) | `ui_db` (`internal: true`) | Opt-in one-shot admin seeder reading `librechat_admin_password` over `stdin` |

*Intentionally omitted upstream services:* `meilisearch` (`SEARCH=false`), `vectordb` / PostgreSQL, `rag_api`, `redis`, and external admin panels are omitted and unnecessary for login, streaming, or persisted history.

## Architecture, Operational Dependencies, and Isolation

- **Minimal viable service topology:**
  - `api`: Non-root FastAPI + LangGraph RCA runtime (`Dockerfile`, `uv sync --frozen --no-dev --no-editable`, Python `3.12`, `UID=10001`, read-only rootfs, `cap_drop: [ALL]`, `no-new-privileges:true`).
  - `ui`: MIT-licensed LibreChat pinned to `v0.8.9` by multi-arch OCI digest (`registry.librechat.ai/librechat-ai/librechat:v0.8.9@sha256:4a3967a47031c1fdba8302a6a341be7671101e40b617f7934ba25aac69fcea2b`), running as non-root `node` (`UID=1000`), read-only rootfs, `cap_drop: [ALL]`, `no-new-privileges:true`.
  - `mongodb`: MongoDB pinned to `8.0` by multi-arch OCI digest (`mongo:8.0@sha256:479e85ced54ffb8a40dade1dcde2da2a9ae858515b9415e9cc54bfb8f0575d01`) persisting LibreChat user accounts and conversation history in `rcannon_mongo_data`.
  - `ui-bootstrap` (opt-in `profiles: ["bootstrap"]`): One-shot container using the pinned LibreChat `v0.8.9` image to seed the initial local `ADMIN` account on a fresh MongoDB.
- **Intentionally omitted upstream services:** `meilisearch` (`SEARCH=false`), `vectordb` / PostgreSQL, `rag_api` (file search/uploads disabled in `librechat.yaml`), `redis`, and any external admin panel are omitted and are not required for login, chat streaming, or conversation persistence.
- **Network separation (`compose.yaml`):**
  - `ui_edge`: publishes only `127.0.0.1:3080` for local loopback UI access (`DOMAIN_CLIENT` and `DOMAIN_SERVER` on loopback; place behind an independent TLS reverse proxy for remote access).
  - `ui_api` (`internal: true`): allows `ui` to reach `api` on port `8000` without exposing `api` on the host.
  - `ui_db` (`internal: true`): connects `ui` and `ui-bootstrap` exclusively to `mongodb` with zero host ports.
  - `api_egress`: provides `api` outbound access to the target Kubernetes API and external LLM provider.
  - `telemetry_net` (`internal: true`): reserved for internal telemetry collection.
- **Credential separation:**
  - `ui` receives only `rcannon_ui_bearer_token` and LibreChat auth/encryption secrets (`librechat_jwt_secret`, `librechat_jwt_refresh_secret`, `librechat_creds_key`, `librechat_creds_iv`) via `/run/secrets/*` (read in `scripts/librechat-entrypoint.sh`).
  - `api` receives `rcannon_ui_bearer_token`, `rcannon_operator_bearer_token`, and optionally `rcannon_llm_api_key` plus read-only kubeconfig mounts.
  - `librechat_admin_password` is mounted only into the opt-in `ui-bootstrap` profile container.
- **UI hardening (`infrastructure/compose/librechat.yaml`):**
  - Exposes a single server-side custom OpenAI endpoint (`RCAnnon` -> `http://api:8000/v1`, `allowedAddresses: ["api:8000"]`) with the three workflow aliases (`rcannon-base`, `rcannon-optimal`, `rcannon-mas`) and `fetch: false`.
  - Enables local email login (`ALLOW_EMAIL_LOGIN=true`) while disabling self-signup (`ALLOW_REGISTRATION=false`), social login/registration, and unverified login.
  - Disables secondary providers, parameters/presets/prompts editing, native tools, agents, web search, file uploads/search, and hidden auto-title LLM calls (`titleConvo: false`), and strips incompatible OpenAI parameters via `dropParams`.
- **Independent health checks:** `api` (`GET /healthz`, `GET /readyz`), `mongodb` (`mongosh db.adminCommand({ ping: 1 })`), and `ui` (`GET /health`) verify service health without depending on the target Kubernetes cluster or external LLM provider.

## Quick Start (Offline Mock Mode, No Target Cluster Required)

1. Generate all required local development secret files:

```sh
./infrastructure/compose/scripts/init-dev-secrets.sh
```

2. Validate the Compose configuration and start the stack:

```sh
docker compose config --quiet
docker compose up -d --build --wait
```

3. Seed the initial local administrator account on the fresh database (idempotent on repeated runs):

```sh
docker compose --profile bootstrap run --rm ui-bootstrap
```

4. Open `http://127.0.0.1:3080` in a browser and sign in with the configured administrator email (`admin@rcannon.local` by default, or `LIBRECHAT_ADMIN_EMAIL`) and the contents of `infrastructure/compose/secrets/librechat_admin_password.secret`. Select `rcannon-base`, `rcannon-optimal`, or `rcannon-mas` and submit an `/rca` prompt (for example `/rca context=lab namespace=demo -- checkout pods failing`).

5. Optionally run a scoped configuration and identity check inside the `api` container:

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
./infrastructure/compose/scripts/init-dev-secrets.sh
RCANNON_IMAGE=ghcr.io/jkuzel02/rcannon:latest docker compose up -d --no-build --wait
docker compose --profile bootstrap run --rm ui-bootstrap
```

Pin to an exact version tag (such as `0.1.3`), `:main` for rolling `main` builds, or an immutable manifest digest (`@sha256:...`). See [docs/container-image.md](../../docs/container-image.md) for the full container runtime contract, environment variables, and provenance verification.

## Enabling Live e-INFRA CZ Inference

1. Create `infrastructure/compose/secrets/llm_api_key.secret` containing your e-INFRA CZ API token (no trailing quotes). Startup fails closed if `RCANNON_PROVIDER_MODE=einfra` is active and the secret file is missing or empty.
2. Start with the provider overlay:

```sh
docker compose -f compose.yaml -f infrastructure/compose/compose.einfra.yaml up -d --wait
```

## Mounting Read-Only Target Kubeconfig and CA Files

1. Place a dedicated read-only kubeconfig and any referenced CA/client certificates in a host directory (default `infrastructure/compose/kubeconfig/`, which is gitignored).
2. Keep certificate paths relative to the kubeconfig file (for example `certificate-authority: ca.crt`) or inline `certificate-authority-data` so the mount remains portable inside `/etc/rcannon/kubeconfig/`.
3. Ensure files are readable by container UID `10001` (`chmod 0750` on the directory, `chmod 0644` or group `10001` `chmod 0640` on config and cert files).
4. Start with the Kubernetes overlay:

```sh
docker compose -f compose.yaml -f infrastructure/compose/compose.kube.yaml up -d --wait
```

Startup rejects kubeconfigs with `exec` plugins, `auth-provider` blocks, or `insecure-skip-tls-verify: true`.

## Linux and macOS Reachability to Kind and Remote Clusters

- **Host-bound Kind API endpoints:** A kubeconfig generated by Kind often uses `https://127.0.0.1:<port>`. Inside a Docker container, `127.0.0.1` refers to the container itself rather than the Docker host.
- **macOS Docker Desktop:** Update the mounted kubeconfig `server` address to `https://host.docker.internal:<port>` when the Kind API certificate SAN includes `host.docker.internal`, or run a TLS-preserving TCP relay on a host-routable interface whose hostname matches a SAN in the API server certificate.
- **Linux Docker Engine:** `compose.kube.yaml` maps `host.docker.internal:host-gateway`. Ensure the Kind cluster `kind/local.yaml` exposes the API on an address reachable from the Docker bridge or add `host.docker.internal` to the API server `certSANs` before regenerating the Kind cluster. Do not set `insecure-skip-tls-verify: true`.
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
