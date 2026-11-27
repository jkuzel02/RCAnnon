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
    assert "tags" not in triggers["push"]

    expected_paths = [
        "Dockerfile",
        ".dockerignore",
        "pyproject.toml",
        "uv.lock",
        ".python-version",
        "rca/**",
        "prompts/base.md",
        "prompts/optimal.md",
        "prompts/mas/*.md",
        "!prompts/**/README.md",
        ".github/workflows/ghcr.yml",
    ]
    assert triggers["pull_request"]["paths"] == expected_paths
    assert triggers["push"]["paths"] == expected_paths
    for excluded_path in (
        "docs/**",
        "README.md",
        "tests/**",
        "scenarios/**",
        "evaluation/**",
        "infrastructure/**",
        "compose.yaml",
    ):
        assert excluded_path not in triggers["push"]["paths"]

    tag_wf = _load_workflow(REPO_ROOT / ".github" / "workflows" / "ghcr-release-tag.yml")
    assert tag_wf["on"]["push"]["tags"] == [
        "[0-9]*.[0-9]*.[0-9]*",
        "v[0-9]*.[0-9]*.[0-9]*",
    ]
    tag_job = tag_wf["jobs"]["publish-tag-image"]
    assert tag_job["uses"] == "./.github/workflows/ghcr.yml"
    assert tag_job["with"]["tag_name"] == "${{ github.ref_name }}"

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
    assert compose["services"]["rcannon"]["image"] == "ghcr.io/jkuzel02/rcannon:latest"
    assert compose["services"]["secrets-init"]["image"] == "python:3.12-slim-bookworm"
    assert "build" not in compose["services"]["rcannon"]
    assert "build" not in compose["services"]["secrets-init"]
    assert set(compose["services"].keys()) == {"secrets-init", "rcannon"}


def test_release_please_extra_files_sync_doc_versions():
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project_version = pyproject["project"]["version"]

    config = json.loads((REPO_ROOT / "release-please-config.json").read_text(encoding="utf-8"))
    extra_files = config["packages"]["."]["extra-files"]
    expected_paths = {
        "docs/container-image.md",
    }
    configured = {entry["path"]: entry["type"] for entry in extra_files}
    assert expected_paths.issubset(configured.keys())
    for rel_path in expected_paths:
        assert configured[rel_path] == "generic"
        content = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        lines = content.splitlines()
        marked_segments: list[str] = []
        in_block = False
        block_lines: list[str] = []
        for line in lines:
            if "x-release-please-start-version" in line:
                in_block = True
                block_lines = []
            elif "x-release-please-end" in line:
                assert in_block
                marked_segments.append("\n".join(block_lines))
                in_block = False
            elif in_block:
                block_lines.append(line)
            elif "x-release-please-version" in line:
                marked_segments.append(line)

        assert not in_block, f"Unclosed x-release-please block in {rel_path}"
        assert marked_segments, f"No x-release-please version markers found in {rel_path}"
        for segment in marked_segments:
            assert project_version in segment, (
                f"Expected version {project_version!r} in marked segment of {rel_path}: {segment!r}"
            )


def test_release_please_triggers_only_on_release_worthy_paths_and_commits():
    config = json.loads((REPO_ROOT / "release-please-config.json").read_text(encoding="utf-8"))
    sections = {entry["type"]: entry for entry in config["changelog-sections"]}
    for non_release_type in ("docs", "chore", "ci", "test"):
        assert sections[non_release_type].get("hidden") is True
    for release_type in ("feat", "fix", "perf", "refactor"):
        assert sections[release_type].get("hidden") is not True

    wf = _load_workflow(REPO_ROOT / ".github/workflows/release-please.yml")
    paths = wf["on"]["push"]["paths"]
    for required_pattern in (
        "rca/**",
        "pyproject.toml",
        "Dockerfile",
        "compose.yaml",
        "prompts/base.md",
        "prompts/optimal.md",
        "prompts/mas/*.md",
        "!prompts/**/README.md",
        "scenarios/**",
        "!scenarios/**/README.md",
        ".release-please-manifest.json",
        "CHANGELOG.md",
    ):
        assert required_pattern in paths
    for excluded_pattern in ("docs/**", "README.md", "tests/**", ".github/**"):
        assert excluded_pattern not in paths
