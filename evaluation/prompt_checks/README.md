# Prompt Checks (Local Quick Start)

All Python runs in this repository use `uv` with Python `3.13` (`.python-version`).
Live evaluations run in CI/CD (`.github/workflows/prompt-checks.yml`); use this
directory only for quick local CLI verification (no output files written).

## 1. Create `.env` in this directory (gitignored)

```dotenv
LLM_AI_EINFRA_API_KEY=your-e-infra-api-key
# Optional (defaults to deepseek-v4.1-flash):
# LLM_AI_EINFRA_MODEL=deepseek-v4.1-flash
```

## 2. Run checks from this directory

```sh
# Offline unit tests (no API key needed)
PYTHONPATH=../.. uv run --with-requirements requirements.txt python -m unittest discover -s . -v

# Live check for one case (CLI output only, no files created)
uv run --env-file .env --with-requirements requirements.txt python run.py --case 01
```

Pass `--output <path>` only if you explicitly want a JSON results file.
See [usage.md](./usage.md) for CI/CD configuration and scoring details.
