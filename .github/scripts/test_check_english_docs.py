"""Unit tests for the English-only Markdown documentation checker."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import check_english_docs as checker  # noqa: E402


class CheckEnglishDocsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.detector = checker.build_detector()

    def test_english_technical_markdown_passes(self) -> None:
        sample = """# Kubernetes Incident Runbook

This document explains how to diagnose CrashLoopBackOff pods and verify
liveness probe timeouts in a local Kind cluster.

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: checkout-api
```

Run `kubectl describe pod checkout-api` and inspect the recent events.
"""
        violations = checker.check_markdown_text(sample, Path("doc.md"), self.detector)
        self.assertEqual(violations, [])

    def test_czech_markdown_fails(self) -> None:
        sample = """# Návrh projektu

Tento dokument popisuje architekturu multiagentního systému pro analýzu
kořenových příčin provozních incidentů v prostředí Kubernetes.
"""
        violations = checker.check_markdown_text(sample, Path("doc.md"), self.detector)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].detected_language, "Czech")
        self.assertEqual(violations[0].line, 3)

    def test_mixed_english_and_czech_paragraph_fails(self) -> None:
        sample = """# Architecture Overview

This service collects sanitized Kubernetes events and pod logs for root cause analysis.

Při řešení incidentů musí uživatel vyhodnocovat velké množství informací z událostí a logů.
"""
        violations = checker.check_markdown_text(sample, Path("mixed.md"), self.detector)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].detected_language, "Czech")
        self.assertEqual(violations[0].line, 5)

    def test_german_markdown_fails(self) -> None:
        sample = """# Fehleranalyse

Diese Dokumentation beschreibt die automatische Ursachenanalyse für Ausfälle
in verteilten Kubernetes-Clustern und deren Überwachung.
"""
        violations = checker.check_markdown_text(sample, Path("de.md"), self.detector)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].detected_language, "German")

    def test_code_blocks_and_short_headings_are_ignored(self) -> None:
        sample = """# RCAnnon

```text
Toto je pouze ukázkový text uvnitř bloku kódu a neměl by selhat.
```

`kubectl get pods -A`
"""
        violations = checker.check_markdown_text(sample, Path("code.md"), self.detector)
        self.assertEqual(violations, [])

    def test_existing_english_repo_docs_pass(self) -> None:
        excluded_non_english = {"docs/proposal.md"}
        excluded_dirs = {
            ".git",
            ".venv",
            "venv",
            ".tox",
            ".nox",
            "node_modules",
            ".pytest_cache",
            "__pycache__",
            "build",
            "dist",
        }
        md_files = sorted(
            p
            for p in REPO_ROOT.rglob("*.md")
            if not any(part in excluded_dirs for part in p.relative_to(REPO_ROOT).parts)
            and p.relative_to(REPO_ROOT).as_posix() not in excluded_non_english
        )
        self.assertTrue(md_files, "Expected Markdown files in repository")
        violations = checker.check_files(md_files, self.detector)
        self.assertEqual(
            violations,
            [],
            f"Unexpected non-English violations in existing English docs: {violations}",
        )


if __name__ == "__main__":
    unittest.main()
