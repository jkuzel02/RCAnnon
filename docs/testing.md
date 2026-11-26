# Testing the LangGraph RCA workflows

From the repository root, use `uv` (Python `3.12` via `.python-version`) and run:

```sh
uv run --with-requirements tests/requirements.txt pytest tests -rx
```

The workflow tests use fake completion responses and run without API
credentials, Kubernetes, or LangGraph. They are expected failures while
`rca.workflows` is absent. Once the module exists, the tests run normally
and implementation errors fail the suite. During test-driven development use
`uv run --with-requirements tests/requirements.txt pytest tests --runxfail` to see the initial failing tests.

## Proposed interface

Implement `rca.workflows.build_workflow(variant, *, complete)` to return a
compiled LangGraph with an `invoke({"context": context})` entry point. The
caller passes the `context` object from a case's `rca.json`. Keep the fixture's
`root-cause` with the evaluator.

Supply the model through the synchronous callable `complete(prompt_path,
payload) -> str`. The prompt path is relative to `prompts/` and the
payload is JSON-compatible. Production can implement this callable with the
[e-INFRA CZ Chat Completions API](https://docs.cerit-sc.cz/en/docs/ai-as-a-service/ai-api).
Tests supply deterministic responses and simulate provider failures through the
same callable.

| Variant | Model calls | Result state |
| --- | --- | --- |
| `base` | `base.md` receives `{"context": context}` | `root-cause` contains the diagnosis text. |
| `optimal` | `optimal.md` receives `{"context": context}` | `root-cause` contains the parsed five-field RCA object. |
| `mas` | Three specialists receive their own fields. The coordinator receives the full context and all three parsed reports keyed by specialist name. | `root-cause` contains the parsed five-field RCA object. |

For MAS, send `resources` and `events` to the configuration specialist,
`logs` and `checks` to the logs specialist, and `metrics`, `time_window`,
and `resource_limits` to the metrics specialist. Each resource limit record
names the workload as `Kind/name` and includes its namespace, container,
requests, and limits. Preserve absent observations as empty lists or a null
time window. The coordinator runs after the three valid reports are ready.

Use the JSON contracts in the corresponding prompt files. Invalid structured
responses should raise `ValueError`. Let completion errors such as provider
timeouts propagate to the caller.

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
