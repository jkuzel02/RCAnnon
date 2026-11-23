"""Tests for bounded JSON evidence bundle import, export, and offline replay."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from rca.api.config import ApiConfig
from rca.api.models import CreateSnapshotRequest
from rca.api.service import RcaApiService
from rca.api.storage import RunStore
from rca.evidence.bundles import (
    BundleScopeDeniedError,
    BundleTooLargeError,
    BundleValidationError,
    export_bundle,
    import_bundle,
)
from rca.evidence.snapshot import CollectionLimits
from rca.workflows import build_workflow

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "contracts" / "snapshot_full.json"


def test_import_and_export_bundle_redacts_secrets_and_drops_secret_objects():
    canary = "secret-bundle-canary-999"
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    payload["context"]["resources"].append(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "db-creds", "namespace": "demo"},
            "data": {"password": "cGFzc3dvcmQ="},
        }
    )
    payload["context"]["logs"].append(
        {"pod": "checkout", "container": "web", "message": f"Authorization: Bearer {canary}"}
    )

    snapshot = import_bundle(json.dumps(payload), extra_secrets=(canary,))
    assert snapshot.source_type == "bundle"
    assert all(res.get("kind") != "Secret" for res in snapshot.context["resources"])
    assert snapshot.coverage["resources"].count == 1
    assert snapshot.coverage["logs"].count == 2

    exported = export_bundle(snapshot, extra_secrets=(canary,))
    assert canary not in exported
    assert "cGFzc3dvcmQ=" not in exported


def test_import_bundle_rejects_oversized_archives_symlinks_and_evaluator_keys(tmp_path: Path):
    limits = CollectionLimits(max_bundle_bytes=128)
    with pytest.raises(BundleTooLargeError):
        import_bundle("x" * 200, limits=limits)

    tar_path = tmp_path / "bundle.tar.gz"
    tar_path.write_bytes(b"{}")
    with pytest.raises(BundleValidationError, match="json"):
        import_bundle(tar_path)

    real_json = tmp_path / "real.json"
    real_json.write_text(FIXTURE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    link_json = tmp_path / "link.json"
    link_json.symlink_to(real_json)
    with pytest.raises(BundleValidationError, match="Symlink"):
        import_bundle(link_json)

    with_ground_truth = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    with_ground_truth["root-cause"] = {"cause": "leak"}
    with pytest.raises(BundleValidationError, match="root-cause"):
        import_bundle(json.dumps(with_ground_truth))


def test_import_bundle_enforces_allowed_scopes_and_truncation():
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    with pytest.raises(BundleScopeDeniedError):
        import_bundle(
            json.dumps(payload),
            allowed_scopes={"other": frozenset({"demo"})},
        )

    payload["context"]["events"] = [{"reason": f"Ev{i}"} for i in range(5)]
    limits = CollectionLimits(max_events=2)
    snapshot = import_bundle(json.dumps(payload), limits=limits)
    assert len(snapshot.context["events"]) == 2
    assert snapshot.coverage["events"].status == "truncated"
    assert snapshot.coverage["events"].truncated is True
    assert snapshot.coverage["events"].count == 2


def test_offline_bundle_replays_into_workflows_and_api_service(tmp_path: Path):
    snapshot = import_bundle(FIXTURE_PATH)
    assert snapshot.source_type == "live" or snapshot.source_type == "bundle"

    base_graph = build_workflow("base", complete=Mock(return_value="OOMKilled on 64Mi limit"))
    result = base_graph.invoke({"context": snapshot.context})
    assert result["root-cause"] == "OOMKilled on 64Mi limit"

    config = ApiConfig(
        ui_tokens=frozenset({"ui-token-1"}),
        operator_tokens=frozenset({"op-token-1"}),
        allowed_scopes={"lab": frozenset({"demo"})},
        db_path=tmp_path / "api.db",
    )
    store = RunStore(config.db_path)
    store.initialize()
    service = RcaApiService(
        config=config,
        store=store,
        complete=Mock(return_value="OOMKilled on 64Mi limit"),
    )
    try:
        stored_snap, _ = service.create_snapshot(
            CreateSnapshotRequest.model_validate(
                {
                    "bundle": {
                        "source_type": "bundle",
                        "context_alias": snapshot.context_alias,
                        "namespaces": snapshot.namespaces,
                        "observed_at": snapshot.observed_at,
                        "context": snapshot.context,
                        "coverage": {k: v.model_dump() for k, v in snapshot.coverage.items()},
                    }
                }
            )
        )
        run = asyncio.run(
            service.execute_run(
                request_id="req-offline-1",
                variant="base",
                snapshot_id=stored_snap.snapshot_id,
            )
        )
        assert run.status == "diagnosed"
        assert run.snapshot_hash == stored_snap.scope_hash
    finally:
        service.shutdown()
        store.close()
