# Testing RCAnnon Workflows and Runtime Contracts

All test suites run offline without live LLM credentials or a Kubernetes cluster, using Python `3.12` (`.python-version`) via `uv`.

## Quick Test Commands

Install dev dependencies once and run the full offline suite from the repository root:

```sh
uv sync
uv run --no-sync pytest tests -rx
```

Run focused subsets while iterating:

```sh
# Workflows & MAS validation
uv run --no-sync pytest tests/test_rca_workflows.py tests/test_mas_workflow.py tests/test_mas_validation.py tests/test_optimal_workflow.py tests/test_base_workflow.py

# Evidence collection & redaction
uv run --no-sync pytest tests/test_evidence_collector.py tests/test_evidence_bundles.py tests/test_evidence_redaction.py

# FastAPI facade, Docker Compose & LibreChat config/scripts
uv run --no-sync pytest tests/test_api.py tests/test_api_storage.py tests/test_compose_deployment.py tests/test_librechat_integration.py

# English-only documentation CI check
uv run --no-project --with-requirements .github/scripts/requirements.txt python .github/scripts/check_english_docs.py --exclude docs/proposal.md README.update.md docs/*.md infrastructure/compose/README.md infrastructure/compose/secrets/README.md
```

For live prompt smoke checks against e-INFRA CZ, see [evaluation/prompt_checks/README.md](../evaluation/prompt_checks/README.md).

## Workflow Contract (`rca.workflows.build_workflow`)

`rca.workflows.build_workflow(variant, *, complete)` returns a compiled LangGraph with an `invoke({"context": context})` entry point. Tests and runtime adapters supply the synchronous callable `complete(prompt_path, payload) -> str`, where `prompt_path` is relative to `prompts/` and `payload` is JSON-compatible.

| Variant | Model calls | Result state |
| --- | --- | --- |
| `base` | `base.md` receives `{"context": context}` | `root-cause` contains the free-text diagnosis. |
| `optimal` | `optimal.md` receives `{"context": context}` | `root-cause` contains the parsed five-field RCA object. |
| `mas` | Three domain specialists run in parallel on partitioned fields; `mas/coordinator.md` receives the full `context` plus all three parsed specialist reports keyed by agent name. | `root-cause` contains the parsed five-field RCA object. |

For `mas`, inputs are partitioned as follows:
- `mas/k8s-config-events.md`: `resources` and `events`
- `mas/logs-connectivity.md`: `logs` and `checks`
- `mas/metrics-telemetry.md`: `metrics`, `time_window`, and `resource_limits` (each resource limit record names the workload as `Kind/name` with namespace, container, requests, and limits)

Absent observations remain empty lists or `null` for `time_window`. Invalid structured responses raise `ValueError`, and provider exceptions (such as timeouts) propagate cleanly to the caller.

## MAS Evidence Validation Boundaries

The production MAS path (`rca.workflows.mas_validation`) enforces deterministic
evidence checks alongside schema validation while preserving strict four-call
execution (`ValueError` and provider exceptions still propagate from `invoke()`).

### Machine-Checked Guarantees

- **Schema and observation structure:** Validates every specialist against
  `REPORT_SCHEMAS` and the coordinator against `RCA`. Reuses shared
  `validate_observations` from `rca.contracts.schemas` so empty `events`, `logs`,
  `checks`, or `metrics` inputs cannot yield populated findings,
  `throttling_detected=true`, non-null `saturation_identified`, or non-`uncollected`
  metric statuses.
- **Affirmative claims over absent or degraded sources:** Flags clauses in
  specialist assessments or coordinator `cause`, `causal_chain`, and
  `supporting_evidence` that assert observed logs, events, metrics, or checks
  when the corresponding source is empty or marked degraded in
  `EvidenceSnapshot.coverage`, while allowing explicit absence disclaimers.
- **Stable reference verification:** Cross-checks cited `Kind/name` objects in
  findings and `supporting_evidence` against identifiers extracted from
  `context` (manifests, workload controller Pod prefixes, events, logs, metrics,
  and referenced Secret/ConfigMap/StorageClass/ServiceAccount names), as well as
  metric names and log sources when those sources are populated.
- **Fail-closed synthesis and payload-free tracing:** Stops strict synthesis
  before invoking `mas/coordinator.md` whenever any specialist fails validation
  or provider execution, emitting a single terminal status and per-agent timing
  and error category metadata to any injected tracer without logging payloads or
  secrets.

### Reviewer-Assessed Properties

- **Semantic causal accuracy:** Determining whether `causal_chain` describes the
  true Kubernetes mechanism rather than a plausible correlation over present data.
- **Misread telemetry values:** Detecting unit errors or faulty interpretation of
  non-empty logs, events, or metric samples that happen to use valid identifiers.
- **Remediation safety and minimality:** Verifying that `remediation` fixes the
  root fault without introducing regressions or unnecessary cluster changes.
- **Qualification nuance:** Judging whether hedged wording over partial evidence
  appropriately reflects diagnostic confidence.
