#!/usr/bin/env python3
"""Enforce English-only prose in Markdown documentation files."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from lingua import Language, LanguageDetector, LanguageDetectorBuilder

FRONTMATTER_RE = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n?", re.DOTALL)
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
HTML_TAG_RE = re.compile(r"<[^>]+>")
IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
REF_LINK_DEF_RE = re.compile(r"^\s*\[[^\]]+\]:\s*\S+.*$")
INLINE_CODE_RE = re.compile(r"`[^`]+`")
URL_RE = re.compile(r"https?://\S+")
TABLE_SEP_RE = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$")
MD_DECORATION_RE = re.compile(r"(^\s*(?:#{1,6}\s+|>\s*|[-*+]\s+|\d+\.\s+))|[*_~|]")
WORD_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿĀ-ž]+")

MIN_WORDS = 6
MIN_ALPHA_CHARS = 30
ENGLISH_CONFIDENCE_THRESHOLD = 0.5
RELATIVE_GAP_THRESHOLD = 0.15


@dataclass(frozen=True)
class TextBlock:
    start_line: int
    text: str


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    detected_language: str
    detected_confidence: float
    english_confidence: float
    snippet: str


def build_detector() -> LanguageDetector:
    return LanguageDetectorBuilder.from_all_spoken_languages().build()


def extract_prose_blocks(markdown: str) -> list[TextBlock]:
    """Extract prose blocks with starting line numbers, stripping code and markup."""
    offset_lines = 0
    frontmatter = FRONTMATTER_RE.match(markdown)
    if frontmatter:
        offset_lines = frontmatter.group(0).count("\n")
        markdown = markdown[frontmatter.end() :]

    # Remove HTML comments while preserving line count.
    def _preserve_newlines(match: re.Match[str]) -> str:
        return "\n" * match.group(0).count("\n")

    markdown = HTML_COMMENT_RE.sub(_preserve_newlines, markdown)

    blocks: list[TextBlock] = []
    current_lines: list[str] = []
    current_start: int | None = None
    in_fence = False
    fence_char = ""
    fence_len = 0

    def flush() -> None:
        nonlocal current_lines, current_start
        if current_lines and current_start is not None:
            joined = " ".join(current_lines).strip()
            if joined:
                blocks.append(TextBlock(start_line=current_start, text=joined))
        current_lines = []
        current_start = None

    for idx, raw_line in enumerate(markdown.splitlines(), start=1 + offset_lines):
        fence_match = FENCE_RE.match(raw_line)
        if fence_match:
            marker = fence_match.group(1)
            if not in_fence:
                flush()
                in_fence = True
                fence_char = marker[0]
                fence_len = len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_len:
                in_fence = False
            continue

        if in_fence:
            continue

        if not raw_line.strip() or TABLE_SEP_RE.match(raw_line) or REF_LINK_DEF_RE.match(raw_line):
            flush()
            continue

        cleaned = _clean_line(raw_line)
        if not cleaned:
            flush()
            continue

        if current_start is None:
            current_start = idx
        current_lines.append(cleaned)

    flush()
    return blocks


def _clean_line(line: str) -> str:
    text = INLINE_CODE_RE.sub(" ", line)
    text = IMAGE_RE.sub(" ", text)
    text = LINK_RE.sub(r"\1", text)
    text = URL_RE.sub(" ", text)
    text = HTML_TAG_RE.sub(" ", text)
    text = MD_DECORATION_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _is_substantial(text: str) -> bool:
    words = WORD_RE.findall(text)
    if len(words) < MIN_WORDS:
        return False
    alpha_chars = sum(len(w) for w in words)
    return alpha_chars >= MIN_ALPHA_CHARS


def _check_text(
    detector: LanguageDetector,
    path: Path,
    line: int,
    text: str,
) -> Violation | None:
    if not _is_substantial(text):
        return None

    confidences = detector.compute_language_confidence_values(text)
    if not confidences:
        return None

    top = confidences[0]
    if top.language == Language.ENGLISH:
        return None

    english_conf = next(
        (item.value for item in confidences if item.language == Language.ENGLISH),
        0.0,
    )
    if english_conf >= ENGLISH_CONFIDENCE_THRESHOLD:
        return None
    if top.value - english_conf < RELATIVE_GAP_THRESHOLD:
        return None

    snippet = text if len(text) <= 100 else f"{text[:97]}..."
    return Violation(
        path=path,
        line=line,
        detected_language=top.language.name.title(),
        detected_confidence=top.value,
        english_confidence=english_conf,
        snippet=snippet,
    )


def check_markdown_text(
    text: str,
    path: Path,
    detector: LanguageDetector,
) -> list[Violation]:
    blocks = extract_prose_blocks(text)
    violations: list[Violation] = []
    for block in blocks:
        violation = _check_text(detector, path, block.start_line, block.text)
        if violation is not None:
            violations.append(violation)

    # Fallback: check combined prose if individual blocks were all too short.
    if not violations and blocks:
        combined = " ".join(b.text for b in blocks)
        violation = _check_text(detector, path, blocks[0].start_line, combined)
        if violation is not None:
            violations.append(violation)

    return violations


def check_files(
    paths: Iterable[Path],
    detector: LanguageDetector | None = None,
) -> list[Violation]:
    active_detector = detector or build_detector()
    violations: list[Violation] = []
    for path in paths:
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8")
        violations.extend(check_markdown_text(content, path, active_detector))
    return violations


def changed_markdown_files(diff_base: str) -> list[Path]:
    result = subprocess.run(
        [
            "git",
            "diff",
            "--diff-filter=ACMR",
            "--name-only",
            "-z",
            f"{diff_base}...HEAD",
        ],
        check=True,
        capture_output=True,
    )
    raw_names = result.stdout.decode("utf-8").split("\0")
    return [
        Path(name)
        for name in raw_names
        if name and name.lower().endswith(".md") and Path(name).is_file()
    ]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check that Markdown documentation files are written in English.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Markdown file paths to validate.",
    )
    parser.add_argument(
        "--diff-base",
        help="Git ref to diff against (checks only added/modified .md files).",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Relative file path to exclude from checks (may be repeated).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.diff_base:
        files = changed_markdown_files(args.diff_base)
    elif args.paths:
        files = list(args.paths)
    else:
        print("Provide file paths or --diff-base <git-ref>.", file=sys.stderr)
        return 2

    excluded = {Path(p).as_posix() for p in args.exclude}
    files = [f for f in files if f.as_posix() not in excluded]

    if not files:
        print("No Markdown files to check.")
        return 0

    violations = check_files(files)
    if not violations:
        print(f"English-only check passed for {len(files)} Markdown file(s).")
        return 0

    in_gha = os.environ.get("GITHUB_ACTIONS") == "true"
    for v in violations:
        message = (
            f"Non-English text detected ({v.detected_language} "
            f"{v.detected_confidence:.2f} vs English {v.english_confidence:.2f}): "
            f"{v.snippet}"
        )
        if in_gha:
            print(f"::error file={v.path.as_posix()},line={v.line}::{message}")
        else:
            print(f"{v.path.as_posix()}:{v.line}: {message}", file=sys.stderr)

    print(
        f"English-only check failed with {len(violations)} violation(s).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
