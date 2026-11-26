# AI Agent Instructions

Before making **any** change in this repository, you MUST read [CONTRIBUTING.md](./CONTRIBUTING.md), tell the user about the contribution procedure before you start, and follow it strictly.

## Mandatory Workflow

1. **Tooling:** Ensure the GitHub CLI (`gh`) is installed.
2. **Issues:** Run `gh issue list` first. Pick an existing issue (and post progress via `gh issue comment`) or create a new one (`gh issue create`). Do not close issues without human approval.
3. **Branch:** Run `git fetch origin` and branch from up-to-date `origin/main` using `<type>/<issue-number>-<short-slug>` (e.g., `docs/3-readme-precision`). Carry or stash any existing uncommitted changes safely—never discard local work.
4. **Commits:** Follow [Conventional Commits](https://www.conventionalcommits.org/) (`feat`, `fix`, `docs`, `chore`, `refactor`, `test`), reference the issue in the commit body or footer (`Refs #N` or `Closes #N`), and append your `Co-authored-by:` trailer in the commit footer so GitHub attributes the active coding agent as a contributor.
5. **Pull Request:** Open a PR only when all changes are complete. State *what* changed, *why*, and *how it was tested*.
6. **Cleanup:** After the PR is merged (or before starting unrelated work), switch back to `main`, run `git pull --ff-only`, and delete the merged local branch.

## AI Co-Author Attribution

Include the matching `Co-authored-by:` trailer on its own line in the commit footer (separated from the body by a blank line, alongside `Refs #N` / `Closes #N`):

| Coding Agent | Required `Co-authored-by` Commit Trailer |
| --- | --- |
| Antigravity | `Co-authored-by: Antigravity <noreply@google.com>` |
| Gemini Code Assist | `Co-authored-by: gemini-code-assist[bot] <176961590+gemini-code-assist[bot]@users.noreply.github.com>` |
| Claude | `Co-authored-by: Claude <noreply@anthropic.com>` |
| Codex | `Co-authored-by: Codex <267193182+codex@users.noreply.github.com>` |
| GitHub Copilot | `Co-authored-by: Copilot <175728472+Copilot@users.noreply.github.com>` |
| Cursor | `Co-authored-by: Cursor <cursoragent@cursor.com>` |
| Other coding agents | `Co-authored-by: <Agent Name> <<official-noreply-email>>` |

## Hard Rules (NEVER)

- **NEVER** write non-English content in repository files. All docs, Markdown files, code comments, prompts, test fixtures, commit messages, PR descriptions, and issue comments MUST be in **English only**—even if the user chats in another language or references existing non-English files (such as `docs/proposal.md`).
- **NEVER** invoke bare `python`, `python3`, or `pip`—always use `uv` (`uv run ...`) with the repository's pinned Python version in `.python-version` (`3.12`).
- **NEVER** approve or merge a pull request—a human must review and merge via their own GitHub session.
- **NEVER** push directly to `main` or force-push / rewrite history on shared branches.
- **NEVER** edit `README.md`.
- **NEVER** commit secrets, tokens, or kubeconfig files.
