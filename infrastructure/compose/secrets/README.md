# Compose Secret Files

This directory holds local runtime secret files mounted into Docker Compose containers under `/run/secrets/`. Secret files matching `*.secret` and `*.txt` are excluded by `.gitignore` and `.dockerignore` and must never be committed.

## Required Files

- `ui_bearer_token.secret`: Shared bearer token used by the `ui` service when calling `api` (`/v1/*`). Must be non-empty and distinct from `operator_bearer_token.secret`.
- `operator_bearer_token.secret`: Separate bearer token authorizing `/api/v1/*` operator routes on `api`. Never mount this file into `ui`.
- `llm_api_key.secret` (required only when enabling `infrastructure/compose/compose.einfra.yaml`): API key for e-INFRA CZ or a compatible OpenAI endpoint, mounted only into `api`.

## Generating Local Development Secrets

Run the helper script from the repository root:

```sh
./infrastructure/compose/scripts/init-dev-secrets.sh
```

Ensure mounted secret files are readable by container UID `10001`. On a local workstation without root privileges, `init-dev-secrets.sh` sets `0644` inside a `0750` directory so non-root containers can read the bind-mounted secrets. On dedicated management hosts, transfer ownership (`chown 10001:10001`) and restrict permissions to `0400` or `0600`.
