# Prompt Checks (Local Quick Start)

All Python runs in this repository use `uv` with Python `3.12` (`.python-version`).
GitHub Actions runs these checks through `.github/workflows/prompt-checks.yml`.
Run the same checks locally from the repository root.

## 1. Create `.env` in this directory (gitignored)

```dotenv
OPENAI_API_KEY=your-openai-api-key
# Optional overrides (defaults to https://api.openai.com/v1 and gpt-4o-mini):
# OPENAI_BASE_URL=https://api.openai.com/v1
# OPENAI_MODEL=gpt-4o-mini
# OPENAI_TIMEOUT_SECONDS=120
```

## 2. Run checks from the repository root

```sh
# Offline unit tests (no API key needed)
uv run pytest tests/prompt_checks

# Live check for one case (CLI output only, no files created)
uv run --env-file tests/prompt_checks/.env python -m tests.prompt_checks.run --case 01
```

Pass `--output <path>` only if you explicitly want a JSON results file.
See [usage.md](./usage.md) for CI/CD configuration and scoring details.
