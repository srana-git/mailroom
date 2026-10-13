"""Playbook parsing and chunking."""

from __future__ import annotations

from pathlib import Path

import pytest

from mailroom.chunking import (
    MAX_CHUNK_CHARS,
    load_playbook_chunks,
    parse_front_matter,
    parse_tags,
)

GOOD = """---
id: PB-XX-001
title: A procedure
owner: Bookkeeping
deadline_rule: Act within 10 days.
language: en
tags: [tax, payment]
---

## When this applies

Something arrives.
"""


def test_front_matter_parses_into_a_map():
    meta, body = parse_front_matter(GOOD)
    assert meta["id"] == "PB-XX-001"
    assert meta["owner"] == "Bookkeeping"
    assert body.startswith("## When this applies")


def test_front_matter_requires_the_opening_marker():
    with pytest.raises(ValueError, match="missing front matter"):
        parse_front_matter("id: PB-XX-001\n---\nbody")


def test_front_matter_requires_the_closing_marker():
    with pytest.raises(ValueError, match="not closed"):
        parse_front_matter("---\nid: PB-XX-001\nbody without a close")


def test_front_matter_rejects_a_line_without_a_colon():
    with pytest.raises(ValueError, match="without ':'"):
        parse_front_matter("---\nid: PB-XX-001\nnonsense\n---\nbody")


def test_quoted_values_are_unquoted():
    meta, _ = parse_front_matter("---\nid: 'PB-XX-001'\n---\nbody")
    assert meta["id"] == "PB-XX-001"


def test_parse_tags_splits_a_bracket_list():
    assert parse_tags("[tax, payment, urgent]") == ["tax", "payment", "urgent"]


def test_parse_tags_on_an_empty_value():
    assert parse_tags("") == []


def test_corpus_loads_and_every_chunk_carries_its_id(repo_root: Path):
    chunks = load_playbook_chunks(repo_root / "data" / "playbook")
    assert len(chunks) >= 30
    for chunk in chunks:
        assert chunk.playbook_id in chunk.text
        assert chunk.title in chunk.text
        assert chunk.source_path.endswith(".md")


def test_corpus_is_within_the_size_the_spec_asks_for(repo_root: Path):
    chunks = load_playbook_chunks(repo_root / "data" / "playbook")
    procedures = {chunk.playbook_id for chunk in chunks}
    assert 30 <= len(procedures) <= 40


def test_no_chunk_exceeds_the_budget_by_much(repo_root: Path):
    chunks = load_playbook_chunks(repo_root / "data" / "playbook")
    oversized = [chunk for chunk in chunks if len(chunk.text) > MAX_CHUNK_CHARS * 2]
    assert oversized == []


def test_a_missing_required_key_is_rejected(tmp_path: Path):
    directory = tmp_path / "playbook"
    directory.mkdir()
    (directory / "bad.md").write_text("---\nid: PB-XX-001\n---\nbody\n", encoding="utf-8")
    with pytest.raises(ValueError, match="front matter needs"):
        load_playbook_chunks(directory)


def test_every_deadline_rule_is_a_single_line(repo_root: Path):
    chunks = load_playbook_chunks(repo_root / "data" / "playbook")
    for chunk in chunks:
        assert "\n" not in chunk.deadline_rule
        assert chunk.deadline_rule.strip() != ""
