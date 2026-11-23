"""Atomic SQLite persistence for bounded snapshots and RCA run metadata."""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path

from rca.contracts.runtime import EvidenceSnapshot, RunResult, utc_now_iso


class RunStore:
    """Thread-safe SQLite store for evidence snapshots and RunResult envelopes."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        clock_fn: Callable[[], float] = time.time,
    ) -> None:
        self._db_path = str(db_path)
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock_fn
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self._db_path,
            timeout=10.0,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        if self._db_path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
        self._initialized = False

    def initialize(self) -> int:
        """Create schema tables, mark stale in-flight runs interrupted, and purge expired snapshots."""
        with self._lock:
            with self._conn:
                self._conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS snapshots (
                        snapshot_id TEXT PRIMARY KEY,
                        scope_hash TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        expires_at REAL NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_snapshots_expires_at
                        ON snapshots (expires_at);
                    CREATE TABLE IF NOT EXISTS runs (
                        run_id TEXT PRIMARY KEY,
                        request_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_runs_status ON runs (status);
                    CREATE INDEX IF NOT EXISTS idx_runs_created_at ON runs (created_at DESC);
                    """
                )
            self._initialized = True
            interrupted_count = self.mark_inflight_runs_interrupted()
            self.purge_expired_snapshots()
            return interrupted_count

    def _ensure_initialized(self) -> None:
        if not self._initialized:
            self.initialize()

    def mark_inflight_runs_interrupted(self) -> int:
        """Transition any persisted 'running' rows to 'interrupted' on startup recovery."""
        with self._lock:
            self._ensure_initialized()
            now_epoch = self._clock()
            now_iso = utc_now_iso()
            with self._conn:
                rows = self._conn.execute(
                    "SELECT run_id, payload_json FROM runs WHERE status = 'running'"
                ).fetchall()
                for row in rows:
                    run = RunResult.model_validate_json(row["payload_json"])
                    updated = run.model_copy(
                        update={
                            "status": "interrupted",
                            "error_code": "process_restarted",
                            "error_message": "Run was interrupted by a service restart.",
                            "completed_at": now_iso,
                        }
                    )
                    self._conn.execute(
                        """
                        UPDATE runs
                        SET status = ?, updated_at = ?, payload_json = ?
                        WHERE run_id = ?
                        """,
                        ("interrupted", now_epoch, updated.model_dump_json(), row["run_id"]),
                    )
                return len(rows)

    def purge_expired_snapshots(self) -> int:
        """Delete all expired snapshots and return deleted row count."""
        with self._lock:
            self._ensure_initialized()
            now_epoch = self._clock()
            with self._conn:
                cur = self._conn.execute(
                    "DELETE FROM snapshots WHERE expires_at <= ?",
                    (now_epoch,),
                )
                return cur.rowcount

    def put_snapshot(
        self,
        snapshot: EvidenceSnapshot,
        *,
        ttl_seconds: int,
        max_snapshots: int,
    ) -> float:
        """Persist a snapshot with TTL and bounded retention."""
        with self._lock:
            self._ensure_initialized()
            now_epoch = self._clock()
            expires_at = now_epoch + ttl_seconds
            payload_json = snapshot.model_dump_json()
            with self._conn:
                self._conn.execute(
                    "DELETE FROM snapshots WHERE expires_at <= ?",
                    (now_epoch,),
                )
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO snapshots (
                        snapshot_id, scope_hash, created_at, expires_at, payload_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot.snapshot_id,
                        snapshot.scope_hash,
                        now_epoch,
                        expires_at,
                        payload_json,
                    ),
                )
                self._conn.execute(
                    """
                    DELETE FROM snapshots
                    WHERE snapshot_id IN (
                        SELECT snapshot_id FROM snapshots
                        ORDER BY created_at DESC, rowid DESC
                        LIMIT -1 OFFSET ?
                    )
                    """,
                    (max_snapshots,),
                )
            return expires_at

    def get_snapshot(self, snapshot_id: str) -> tuple[EvidenceSnapshot, float] | None:
        """Fetch an unexpired snapshot and its expiration epoch timestamp."""
        with self._lock:
            self._ensure_initialized()
            now_epoch = self._clock()
            with self._conn:
                self._conn.execute(
                    "DELETE FROM snapshots WHERE expires_at <= ?",
                    (now_epoch,),
                )
                row = self._conn.execute(
                    "SELECT payload_json, expires_at FROM snapshots WHERE snapshot_id = ?",
                    (snapshot_id,),
                ).fetchone()
            if row is None:
                return None
            return EvidenceSnapshot.model_validate_json(row["payload_json"]), float(row["expires_at"])

    def put_run(self, run: RunResult, *, max_runs: int) -> None:
        """Persist a new RunResult and trim oldest non-running records beyond max_runs."""
        with self._lock:
            self._ensure_initialized()
            now_epoch = self._clock()
            with self._conn:
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO runs (
                        run_id, request_id, status, created_at, updated_at, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run.run_id,
                        run.request_id,
                        run.status,
                        now_epoch,
                        now_epoch,
                        run.model_dump_json(),
                    ),
                )
                self._conn.execute(
                    """
                    DELETE FROM runs
                    WHERE status != 'running' AND run_id IN (
                        SELECT run_id FROM runs
                        ORDER BY created_at DESC, rowid DESC
                        LIMIT -1 OFFSET ?
                    )
                    """,
                    (max_runs,),
                )

    def update_run(self, run: RunResult) -> None:
        """Atomically update an existing RunResult record."""
        with self._lock:
            self._ensure_initialized()
            now_epoch = self._clock()
            with self._conn:
                cur = self._conn.execute(
                    """
                    UPDATE runs
                    SET status = ?, updated_at = ?, payload_json = ?
                    WHERE run_id = ?
                    """,
                    (run.status, now_epoch, run.model_dump_json(), run.run_id),
                )
                if cur.rowcount == 0:
                    self._conn.execute(
                        """
                        INSERT INTO runs (
                            run_id, request_id, status, created_at, updated_at, payload_json
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run.run_id,
                            run.request_id,
                            run.status,
                            now_epoch,
                            now_epoch,
                            run.model_dump_json(),
                        ),
                    )

    def get_run(self, run_id: str) -> RunResult | None:
        """Return a RunResult by run_id, or None if not found."""
        with self._lock:
            self._ensure_initialized()
            row = self._conn.execute(
                "SELECT payload_json FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            return RunResult.model_validate_json(row["payload_json"])

    def list_runs(self, *, limit: int = 20) -> list[RunResult]:
        """Return recent RunResult records ordered newest first."""
        with self._lock:
            self._ensure_initialized()
            rows = self._conn.execute(
                "SELECT payload_json FROM runs ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [RunResult.model_validate_json(row["payload_json"]) for row in rows]

    def ping(self) -> bool:
        """Verify SQLite store connectivity for /readyz."""
        with self._lock:
            self._ensure_initialized()
            row = self._conn.execute("SELECT 1").fetchone()
            return row is not None and row[0] == 1

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        with self._lock:
            self._conn.close()
