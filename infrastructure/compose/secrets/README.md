# Compose Secret Files

This directory holds local runtime secret files mounted into Docker Compose containers under `/run/secrets/`. Secret files matching `*.secret` and `*.txt` are excluded by `.gitignore` and `.dockerignore` and must never be committed.

## Required Files

- `ui_bearer_token.secret`: Bearer token used by OpenAI-compatible chat clients or external UIs when calling `rcannon` (`/v1/*`). Must be non-empty and distinct from `operator_bearer_token.secret`.
- `operator_bearer_token.secret`: Separate bearer token authorizing `/api/v1/*` operator routes on `rcannon`.
- `llm_api_key.secret` (required only when enabling `infrastructure/compose/compose.openai.yaml`): API key for OpenAI or a compatible Chat Completions endpoint, mounted only into `rcannon`.

## Generating Local Development Secrets

Run the helper script from the repository root:

```sh
./infrastructure/compose/scripts/init-dev-secrets.sh
```

The script preserves any existing non-empty `.secret` files and creates missing ones with exact required formats.

## File Ownership and Permissions

- `rcannon` runs as UID `10001:10001` and needs read access to `ui_bearer_token.secret`, `operator_bearer_token.secret`, and (when enabled) `llm_api_key.secret`.
- On a local workstation without root privileges, `init-dev-secrets.sh` sets `0644` inside a `0750` directory so non-root containers can read bind-mounted secrets. On dedicated management hosts, transfer ownership to the container UID (`chown 10001:10001`) and restrict permissions to `0400` or `0440`.
