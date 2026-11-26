# Compose Secret Files

This directory holds local runtime secret files mounted into Docker Compose containers under `/run/secrets/`. Secret files matching `*.secret` and `*.txt` are excluded by `.gitignore` and `.dockerignore` and must never be committed.

## Required Files

### Core API and UI Bridge

- `ui_bearer_token.secret`: Shared bearer token used by the `ui` service (LibreChat custom `RCAnnon` endpoint) when calling `api` (`/v1/*`). Must be non-empty and distinct from `operator_bearer_token.secret`.
- `operator_bearer_token.secret`: Separate bearer token authorizing `/api/v1/*` operator routes on `api`. Never mount this file into `ui`.
- `llm_api_key.secret` (required only when enabling `infrastructure/compose/compose.einfra.yaml`): API key for e-INFRA CZ or a compatible OpenAI endpoint, mounted only into `api`.

### LibreChat Authentication and Bootstrap

- `librechat_jwt_secret.secret`: Non-empty single-line signing secret for LibreChat access JWTs (`JWT_SECRET`).
- `librechat_jwt_refresh_secret.secret`: Non-empty single-line signing secret for LibreChat refresh JWTs (`JWT_REFRESH_SECRET`).
- `librechat_creds_key.secret`: Exact 64-character hexadecimal AES-256 key (`CREDS_KEY`, 32 bytes hex-encoded).
- `librechat_creds_iv.secret`: Exact 32-character hexadecimal initialization vector (`CREDS_IV`, 16 bytes hex-encoded).
- `librechat_admin_password.secret`: Single-line initial local administrator password (minimum 12 characters) read only by the opt-in `ui-bootstrap` profile and piped via `stdin` to `npm run create-user`. Once initial bootstrap completes, you may remove or rotate this file outside Compose runs if you do not plan to re-run `--profile bootstrap`.

## Generating Local Development Secrets

Run the helper script from the repository root:

```sh
./infrastructure/compose/scripts/init-dev-secrets.sh
```

The script preserves any existing non-empty `.secret` files and creates missing ones with exact required formats.

## File Ownership and Permissions

- `api` runs as UID `10001:10001` and needs read access to `ui_bearer_token.secret`, `operator_bearer_token.secret`, and (when enabled) `llm_api_key.secret`.
- `ui` (LibreChat `v0.8.9`) and `ui-bootstrap` run as UID `1000:1000` (`node`) and need read access to `ui_bearer_token.secret` and `librechat_*.secret`.
- On a local workstation without root privileges, `init-dev-secrets.sh` sets `0644` inside a `0750` directory so non-root containers can read bind-mounted secrets. On dedicated management hosts, transfer ownership to the respective container UID (`chown 10001:10001` for `api`-only secrets, `chown 1000:1000` for LibreChat secrets, or a shared read group on `ui_bearer_token.secret`) and restrict permissions to `0400` or `0440`.
