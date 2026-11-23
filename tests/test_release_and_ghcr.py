"""Contract tests for GHCR publishing, container image docs, and automated CHANGELOG."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_workflow(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if True in data and "on" not in data:
        data["on"] = data.pop(True)
    return data


def test_version_alignment_between_pyproject_and_release_please_manifest():
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project_version = pyproject["project"]["version"]

    manifest = json.loads(
        (REPO_ROOT / ".release-please-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["."] == project_version

    config = json.loads((REPO_ROOT / "release-please-config.json").read_text(encoding="utf-8"))
    assert config["release-type"] == "python"
    assert config["package-name"] == "rcannon"
    assert config["changelog-path"] == "CHANGELOG.md"
    assert config["include-v-in-tag"] is False
    assert config["include-component-in-tag"] is False


def test_ghcr_workflow_builds_safely_on_pr_and_publishes_attested_multiarch_images():
    wf = _load_workflow(REPO_ROOT / ".github" / "workflows" / "ghcr.yml")
    triggers = wf["on"]
    assert "pull_request" in triggers
    assert "push" in triggers
    assert "workflow_call" in triggers
    assert "workflow_dispatch" in triggers
    assert "main" in triggers["push"]["branches"]
    assert triggers["push"]["tags"]

    assert wf["permissions"] == {"contents": "read"}
    job = wf["jobs"]["container"]
    assert job["permissions"] == {
        "contents": "read",
        "packages": "write",
        "id-token": "write",
        "attestations": "write",
    }

    steps_by_uses = {s.get("uses"): s for s in job["steps"] if "uses" in s}
    assert "docker/login-action@v3" in steps_by_uses
    assert steps_by_uses["docker/login-action@v3"]["with"]["registry"] == "ghcr.io"
    assert "actions/attest-build-provenance@v2" in steps_by_uses

    meta_labels = steps_by_uses["docker/metadata-action@v5"]["with"]["labels"]
    assert "org.opencontainers.image.documentation=" in meta_labels
    assert "org.opencontainers.image.source=" in meta_labels
    assert "org.opencontainers.image.licenses=Apache-2.0" in meta_labels

    build_steps = [s for s in job["steps"] if s.get("uses") == "docker/build-push-action@v6"]
    assert len(build_steps) == 2
    pr_build, publish_build = build_steps
    assert pr_build["with"]["push"] is False
    assert pr_build["with"]["platforms"] == "linux/amd64"
    assert publish_build["with"]["push"] is True
    assert publish_build["with"]["platforms"] == "linux/amd64,linux/arm64"
    assert publish_build["with"]["sbom"] is True
    assert publish_build["with"]["provenance"] == "mode=max"


def test_release_please_workflow_chains_ghcr_release_publish():
    wf = _load_workflow(REPO_ROOT / ".github" / "workflows" / "release-please.yml")
    triggers = wf["on"]
    assert "main" in triggers["push"]["branches"]

    rp_job = wf["jobs"]["release-please"]
    assert rp_job["permissions"] == {"contents": "write", "pull-requests": "write"}
    assert any(
        s.get("uses") == "googleapis/release-please-action@v4" for s in rp_job["steps"]
    )

    pub_job = wf["jobs"]["publish-release-image"]
    assert pub_job["needs"] == "release-please"
    assert "release_created == 'true'" in pub_job["if"]
    assert pub_job["uses"] == "./.github/workflows/ghcr.yml"
    assert pub_job["with"]["tag_name"] == "${{ needs.release-please.outputs.tag_name }}"


def test_container_docs_changelog_dockerfile_labels_and_compose_image_var():
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [0.1.0]" in changelog

    image_doc = (REPO_ROOT / "docs" / "container-image.md").read_text(encoding="utf-8")
    assert "ghcr.io/jkuzel02/rcannon" in image_doc
    assert "10001:10001" in image_doc
    assert "0.1.0" in image_doc

    contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "pyproject.toml" in contributing
    assert ".release-please-manifest.json" in contributing
    assert "CHANGELOG.md" in contributing

    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    for label in (
        "org.opencontainers.image.title=",
        "org.opencontainers.image.description=",
        "org.opencontainers.image.source=",
        "org.opencontainers.image.documentation=",
        "org.opencontainers.image.licenses=",
    ):
        assert label in dockerfile

    compose = yaml.safe_load((REPO_ROOT / "compose.yaml").read_text(encoding="utf-8"))
    assert compose["services"]["api"]["image"] == "${RCANNON_IMAGE:-rcannon-api:local}"
    assert compose["services"]["ui"]["image"] == "${RCANNON_IMAGE:-rcannon-api:local}"
