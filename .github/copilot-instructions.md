# Copilot Instructions

Read and follow [AGENTS.md](../AGENTS.md) and [CONTRIBUTING.md](../CONTRIBUTING.md) before making any changes.

## Key Rules

- **English only:** Write all repository files (docs, Markdown, code comments, prompts, fixtures, commit messages, PR descriptions, and issue comments) strictly in **English**, even if the conversation or existing legacy files (such as `docs/proposal.md`) are in another language.
- **Python runtime:** Always use `uv` (`uv run ...`) with Python `3.12` (pinned in `.python-version`); never use bare `python` or `pip`.
- Follow the mandatory GitHub workflow: issue (`gh issue list` / `gh issue create`) -> branch (`<type>/<issue>-<slug>`) -> Conventional Commits -> PR for human review.
- **Co-author attribution:** Append `Co-authored-by: Copilot <175728472+Copilot@users.noreply.github.com>` (see [AGENTS.md](../AGENTS.md)) in every commit footer so GitHub attributes Copilot as a contributor.
- Never approve or merge a PR, never push to `main`, never edit `README.md`, and never commit secrets or kubeconfigs.
