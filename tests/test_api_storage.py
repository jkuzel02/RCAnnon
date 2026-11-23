"""SQLite persistence, restart recovery, and snapshot TTL tests for RunStore."""

from __future__ import annotations

from pathlib import Path

from rca.api.storage import RunStore
from rca.contracts.runtime import (
    EvidenceSnapshot,
    RunResult,
    SourceCoverage,
    compute_scope_hash,
    utc_now_iso,
)


def _sample_snapshot(snapshot_id: str, incident: str = "OOMKilled") -> EvidenceSnapshot:
    context = {
        "incident": incident,
        "time_window": None,
        "resources": [{"kind": "Deployment", "metadata": {"name": "api"}}],
        "events": [],
        "logs": [],
        "metrics": [],
        "checks": [],
    }
    return EvidenceSnapshot(
        snapshot_id=snapshot_id,
        scope_hash=compute_scope_hash("lab", ["demo"], context),
        source_type="bundle",
        context_alias="lab",
        namespaces=["demo"],
        collected_at=utc_now_iso(),
        observed_at=None,
        context=context,
        coverage={"resources": SourceCoverage(status="observed", count=1)},
    )


def _sample_run(run_id: str, status: str) -> RunResult:
    return RunResult(
        request_id=f"req_{run_id}",
        run_id=run_id,
        variant="mas",
        snapshot_id="snap_1",
        snapshot_hash="hash_1",
        status=status,  # type: ignore[arg-type]
        result="ok" if status == "diagnosed" else None,
        created_at=utc_now_iso(),
        completed_at=utc_now_iso() if status != "running" else None,
    )


def test_store_restart_preserves_completed_and_interrupts_inflight(tmp_path: Path) -> None:
    db_file = tmp_path / "rcannon.db"
    store1 = RunStore(db_file)
    assert store1.initialize() == 0

    completed = _sample_run("run_done", "diagnosed")
    inflight = _sample_run("run_inflight", "running")
    store1.put_run(completed, max_runs=50)
    store1.put_run(inflight, max_runs=50)
    store1.close()

    # Re-open against the same SQLite database simulating process restart.
    store2 = RunStore(db_file)
    try:
        recovered = store2.initialize()
        assert recovered == 1

        persisted_done = store2.get_run("run_done")
        assert persisted_done is not None
        assert persisted_done.status == "diagnosed"
        assert persisted_done.result == "ok"

        persisted_inflight = store2.get_run("run_inflight")
        assert persisted_inflight is not None
        assert persisted_inflight.status == "interrupted"
        assert persisted_inflight.error_code == "process_restarted"
        assert persisted_inflight.completed_at is not None
    finally:
        store2.close()


def test_snapshot_ttl_expiration_and_bounded_eviction(tmp_path: Path) -> None:
    now = [1000.0]
    store = RunStore(tmp_path / "ttl.db", clock_fn=lambda: now[0])
    try:
        store.initialize()
        snap_a = _sample_snapshot("snap_a")
        snap_b = _sample_snapshot("snap_b")
        snap_c = _sample_snapshot("snap_c")

        store.put_snapshot(snap_a, ttl_seconds=10, max_snapshots=2)
        now[0] = 1002.0
        store.put_snapshot(snap_b, ttl_seconds=20, max_snapshots=2)

        assert store.get_snapshot("snap_a") is not None
        assert store.get_snapshot("snap_b") is not None

        # Adding a 3rd snapshot with max_snapshots=2 evicts the oldest (snap_a).
        now[0] = 1004.0
        store.put_snapshot(snap_c, ttl_seconds=20, max_snapshots=2)
        assert store.get_snapshot("snap_a") is None
        assert store.get_snapshot("snap_b") is not None
        assert store.get_snapshot("snap_c") is not None

        # Advancing clock past snap_b's TTL (1022.0) expires snap_b while snap_c (1024.0) stays.
        now[0] = 1022.5
        assert store.get_snapshot("snap_b") is None
        assert store.get_snapshot("snap_c") is not None

        # Run retention respects max_runs while preserving newer rows.
        for idx in range(5):
            now[0] += 1.0
            store.put_run(_sample_run(f"run_{idx}", "diagnosed"), max_runs=3)
        runs = store.list_runs(limit=10)
        assert [r.run_id for r in runs] == ["run_4", "run_3", "run_2"]
    finally:
        store.close()
