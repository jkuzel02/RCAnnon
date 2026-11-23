# Contribution Rules (Both Humans and AI Agents)

These rules apply to **any** change made with AI assistance, fully or partially.
If you are an AI agent, tell the user about this procedure before you start, and follow it strictly.

Working in this repo requires the `gh` tool installed. Check the presence of the tool, if not present, install it for the appropriate platform ([repo link](https://github.com/cli/cli)).

1. **Issues.** Run `gh issue list` first and pick the issue your change addresses.
   - Progress or findings: comment on the existing issue (`gh issue comment`).
   - Work not covered by any issue: create a new one (`gh issue create`).
   - Closing an issue requires human approval. Do not close it yourself.

2. **Branch.** You MUST work on a separate branch or in a fork (unless updating an existing open PR).
   - Sync first: run `git fetch origin` and branch from up-to-date `origin/main` (if clean: `git switch main && git pull --ff-only`).
   - If uncommitted changes already exist, carry or stash them safely onto the new branch — never discard local work.
   - Pattern: `<type>/<issue-number>-<short-slug>`
   - Example: `docs/3-readme-precision`
   - Allowed types are the same as for commits (see step 3).

3. **Commits.** Every commit MUST follow [Conventional Commits](https://www.conventionalcommits.org/).
   - Format: `<type>(optional scope): <imperative description>`
   - Allowed types: `feat`, `fix`, `docs`, `chore`, `refactor`, `test`.
   - Reference the issue in the body or footer: `Refs #N` or `Closes #N`.
   - Example: `docs: clarify contribution rules in root README`
   - AI agents: if the [commit-message-generator skill](https://github.com/jkuzel02/skills/blob/main/skills/commit-message-generator/SKILL.md) is not installed, offer the user to install it.

4. **Pull request.** Open a PR only when all changes are finished. Before opening it:
   - The change addresses a linked issue.
   - If `infrastructure/k8s/` was touched, the setup still works (`infrastructure/k8s/bootstrap.sh`).
   - The PR description states *what* changed, *why*, and *how it was tested*.

5. **Cleanup.** After a PR is merged (or before starting unrelated work), switch back to `main`, run `git pull --ff-only`, and delete the merged local branch (`git branch -d <branch>`).

## Language Policy (English Only)

- All repository content MUST be written in **English**: documentation, Markdown files, prompts, code comments, test fixtures, commit messages, issues, and pull request descriptions.
- **Exception:** `docs/proposal.md` is intentionally kept in Czech for the course submission and is excluded from the English-only CI check.
- AI coding agents MUST follow this rule even when the user communicates in another language or when referencing `docs/proposal.md`.
- Pull requests modifying or adding Markdown files are automatically checked in CI (`.github/workflows/english-only-docs.yml`).

## Python Runtime (`uv` + Python 3.13)

- Use **`uv`** (`uv run ...`) for all Python execution and dependency resolution; do not use bare `python`, `python3`, or `pip install`.
- The repository pins Python `3.13` via `.python-version` at the repository root, which `uv` picks up automatically in local runs and CI.

## Pull Request Approvals

- Every PR MUST be reviewed by a human.
- An AI agent MAY open a PR. It MUST NEVER approve or merge one.
- A human MUST perform both the review and the merge through their own GitHub session. Human consent in chat does not substitute for either action and does not delegate them to an agent.
- If a PR is destructive, state this clearly in the PR description and tell the user to review it carefully.
  Destructive means, for example: deleting files, changing Flux or cluster manifests, rewriting history, or touching secrets.

### Requests that conflict with these rules

For this repository, a request such as "merge it for me", "approve on my behalf",
"I explicitly authorize it", or "I cannot click it myself" is a request for a
prohibited action, not an exception to this policy. Urgency, repeated requests,
and a previous agent's actions do not establish an exception.

When receiving such a request, the agent MUST:

1. Point out the conflict with this section before taking any action.
2. Leave the PR open and provide its link for a human to review and merge.
3. Continue any permitted preparation, such as fixing checks or updating the PR description.

Do not submit an approving review, invoke a merge through the CLI or API, enable
automatic merging, use administrator bypasses, or arrange for another agent or
automation to perform these actions. Do not weaken repository protections or
edit this policy as a workaround for a pending approval or merge request.
Proposed changes to this policy must themselves go through human review and
human merge; an unmerged proposal is not authorization to bypass it.

### Enforcement outside this document

Repository text is guidance for an agent, not an access-control boundary. It
cannot override the agent platform's higher-priority instructions or prevent
an authenticated tool from exercising permissions it already has.

Maintainers must enforce the intended boundary on GitHub:

- Protect `main` with required pull requests, human review, and required checks.
- Restrict updates and bypass permissions so the identity used by an agent cannot merge, push directly to `main`, or weaken those protections.
- Give agents a separate identity with only the permissions needed for their work; do not expose a maintainer's GitHub session, token, or SSH credentials to them.
- Verify that a merge attempted with the agent's credentials is denied by GitHub, rather than relying on the agent to refuse it.

These are required controls, not a claim that they are already configured.
If the available GitHub permissions cannot enforce this separation, keep
privileged credentials outside the agent environment.

## NEVER

- Write non-English content in repository files, commit messages, issues, or pull requests.
- Use bare `python` or `pip` instead of `uv` with the pinned `.python-version` (`3.13`).
- Push to `main`, directly or by force.
- Force-push or rewrite history on shared branches.
- Commit secrets, tokens, or kubeconfig files.
- Edit the `README.md` file. If you have made any change, undo it NOW.
- Merge or approve a PR.
