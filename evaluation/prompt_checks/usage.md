# Testing RCA prompts

Run the offline checks from the repository root with `uv` (Python `3.13` via `.python-version`):

```sh
uv run --with-requirements evaluation/prompt_checks/requirements.txt \
  python -m unittest discover -s evaluation/prompt_checks -v
```

For quick local live checks from inside `evaluation/prompt_checks/`, see [README.md](./README.md).

These checks use the real cases and substitute deterministic model responses.
GitHub Actions runs them on relevant pull requests and pushes to `main`.

## Live checks on e-INFRA CZ

Configure the GitHub Actions secret `LLM_AI_EINFRA_API_KEY` using an API key
authorized for the workflow. The optional Actions variable
`LLM_AI_EINFRA_MODEL` overrides the evaluator's default model
(`deepseek-v4.1-flash`). See the
[e-INFRA CZ API guide](https://docs.cerit-sc.cz/en/docs/ai-as-a-service/ai-api).

In GitHub Actions, open **RCA prompt checks** and select **Run workflow**.
The default input runs case `01`. Clear the case input to run all 16 cases.
For automatic live runs on relevant pushes to `main`, set the repository
variable `RCA_LIVE_PROMPT_CHECKS` to `true`. A full run makes up to 96 sequential
requests. Check the
[provider's benchmarking guidance](https://docs.cerit-sc.cz/en/docs/ai-as-a-service/chat-ai#shared-resources-and-benchmarking)
before running the full benchmark.

Locally, supply the API key via `.env` (or the environment) and run from the
repository root for CLI-only output (pass `--output <path>` only when you want
to write a JSON file):

```sh
uv run --env-file evaluation/prompt_checks/.env \
  --with-requirements evaluation/prompt_checks/requirements.txt \
  python -m evaluation.prompt_checks.run --case 01
```

## Interpreting results

Each case sends its incident context to the base and optimal prompts. MAS
sends the corresponding fields to each of the three specialists and passes
their validated reports and the complete context to the coordinator. The
per-case expected answers and scoring terms stay with the evaluator.

The baseline accepts free text. The optimal prompt, coordinator and
specialists must return the JSON structures specified in their prompts.
The specialists' structured findings must reflect which logs, events,
checks and metrics are present in the input.
The three final diagnoses are checked for case-specific terms from
`expectations.json`. A failed check or a blocked coordinator fails the
live job. Each attempted check records the response, duration, available
token usage and failure details in the `rca-prompt-results` artifact.

The scoring terms catch generic or wrong diagnoses and can miss valid
paraphrases. Review the artifact to verify causal reasoning, supporting
evidence and remediation. The current cases include fault manifests and
leave runtime observations empty. Treat claims about observed logs, events,
metrics and checks accordingly.
