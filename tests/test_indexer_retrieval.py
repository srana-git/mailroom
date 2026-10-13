"""Index building and the retrieval path, using the offline embedding."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mailroom.config import Settings
from mailroom.fake import FakeBackend, hash_embedding
from mailroom.indexer import META_NAME, TABLE_NAME, IndexError_, PlaybookIndex
from mailroom.models import RetrievalHit
from mailroom.retrieval import Retriever, dedupe_by_playbook, forced_hits


def test_hash_embedding_is_deterministic_and_normalised():
    first = hash_embedding("final notice from the tax agency", 64)
    second = hash_embedding("final notice from the tax agency", 64)
    assert first == second
    assert len(first) == 64
    assert abs(sum(value * value for value in first) - 1.0) < 1e-9


def test_hash_embedding_of_empty_text_is_the_zero_vector():
    assert set(hash_embedding("", 32)) == {0.0}


def test_similar_texts_land_closer_than_unrelated_ones():
    query = hash_embedding("annual declaration registry filing", 1024)
    near = hash_embedding("registry annual declaration must be filed", 1024)
    far = hash_embedding("electricity invoice for the warehouse", 1024)

    def cosine(a, b):
        return sum(x * y for x, y in zip(a, b, strict=True))

    assert cosine(query, near) > 0.0
    assert cosine(query, near) > cosine(query, far)


def test_building_the_index_writes_a_table_and_a_meta_file(
    settings: Settings, backend: FakeBackend
):
    index = PlaybookIndex(settings, backend)
    meta = index.build(force=True, verbose=False)
    assert meta["chunks"] > 0
    assert meta["procedures"] >= 30
    assert meta["embed_model"] == settings.embed_model
    assert meta["backend"] == "fake"
    assert (settings.index_dir / META_NAME).exists()


def test_the_meta_file_names_the_table_and_the_width(settings: Settings, backend: FakeBackend):
    index = PlaybookIndex(settings, backend)
    meta = index.build(force=True, verbose=False)
    on_disk = json.loads((settings.index_dir / META_NAME).read_text(encoding="utf-8"))
    assert on_disk == meta
    assert on_disk["table"] == TABLE_NAME
    assert on_disk["dimension"] > 0


def test_a_stale_index_is_refused_rather_than_read(settings: Settings, backend: FakeBackend):
    PlaybookIndex(settings, backend).build(force=True, verbose=False)
    other = Settings.build(
        root=settings.root,
        backend="fake",
        state_dir=settings.state_dir,
        index_dir=settings.index_dir,
        queue_path=settings.queue_path,
        runs_dir=settings.runs_dir,
        embed_model="embed-of-the-future",
    )
    with pytest.raises(IndexError_, match="--force"):
        PlaybookIndex(other, backend).table()


def test_building_without_a_playbook_fails_loudly(tmp_path: Path, backend: FakeBackend):
    settings = Settings.build(
        root=tmp_path, backend="fake", playbook_dir=tmp_path / "empty-playbook"
    )
    (tmp_path / "empty-playbook").mkdir()
    with pytest.raises(IndexError_, match="no playbook files"):
        PlaybookIndex(settings, backend).build(force=True, verbose=False)


def test_rebuilding_replaces_the_table(settings: Settings, backend: FakeBackend):
    index = PlaybookIndex(settings, backend)
    first = index.build(force=True, verbose=False)
    second = index.build(force=True, verbose=False)
    assert first["chunks"] == second["chunks"]


def test_retrieval_returns_at_most_the_final_top_k(retriever: Retriever):
    hits = retriever.search("registry annual declaration", top_k=3)
    assert 0 < len(hits) <= 3


def test_retrieval_scores_are_better_than_zero_and_ordered(retriever: Retriever):
    hits = retriever.search("final notice from the tax agency", top_k=3)
    assert hits
    scores = [hit.score for hit in hits]
    assert scores == sorted(scores, reverse=True)


def test_the_reranked_stage_is_recorded(retriever: Retriever):
    hits = retriever.search("payroll remittance discrepancy", top_k=2)
    assert all(hit.stage == "rerank" for hit in hits)


def test_without_rerank_the_vector_stage_is_recorded(
    settings: Settings, backend: FakeBackend, index: PlaybookIndex
):
    retriever = Retriever(settings, backend, index=index, use_rerank=False)
    hits = retriever.search("payroll remittance discrepancy", top_k=2)
    assert all(hit.stage == "vector" for hit in hits)


def test_vector_only_retrieval_returns_more_than_one_candidate_pool(retriever: Retriever):
    """The query path is top 20 then rerank to 3, so the pool must be larger."""
    assert retriever.settings.retrieval_top_k == 20
    assert retriever.settings.final_top_k == 3


def test_a_query_with_no_lexical_overlap_still_returns_something(retriever: Retriever):
    hits = retriever.search("zzzz qqqq", top_k=3)
    assert len(hits) == 3


def test_dedupe_keeps_the_best_chunk_per_procedure():
    hits = [
        RetrievalHit(
            playbook_id="PB-A", title="a", chunk_id="PB-A#0", text="x", score=0.9, stage="rerank"
        ),
        RetrievalHit(
            playbook_id="PB-A", title="a", chunk_id="PB-A#1", text="y", score=0.8, stage="rerank"
        ),
        RetrievalHit(
            playbook_id="PB-B", title="b", chunk_id="PB-B#0", text="z", score=0.7, stage="rerank"
        ),
    ]
    unique = dedupe_by_playbook(hits)
    assert [hit.playbook_id for hit in unique] == ["PB-A", "PB-B"]
    assert unique[0].score == 0.9


def test_dedupe_can_limit():
    hits = [
        RetrievalHit(
            playbook_id=name, title=name, chunk_id=name, text="t", score=0.5, stage="vector"
        )
        for name in ("PB-A", "PB-B", "PB-C")
    ]
    assert len(dedupe_by_playbook(hits, limit=2)) == 2


def test_a_missing_index_metadata_block_is_reported(settings: Settings, backend: FakeBackend):
    settings.index_dir.mkdir(parents=True, exist_ok=True)
    with pytest.raises(IndexError_, match="no index metadata"):
        PlaybookIndex(settings, backend).table()


# -- procedures forced by a deterministic scan -----------------------------


def test_forced_hits_name_the_requested_procedures_in_order(settings: Settings):
    hits = dedupe_by_playbook(forced_hits(settings.playbook_dir, ["PB-SEC-001", "PB-SEC-002"]))
    assert [hit.playbook_id for hit in hits] == ["PB-SEC-001", "PB-SEC-002"]
    assert all(hit.stage == "forced" for hit in hits)
    assert all(hit.score == 1.0 for hit in hits)


def test_forced_hits_carry_the_procedure_text(settings: Settings):
    hits = forced_hits(settings.playbook_dir, ["PB-SEC-002"])
    assert hits
    assert hits[0].title == "Instructions embedded in a letter"
    assert "PB-SEC-002" in hits[0].text


def test_forced_hits_are_empty_for_no_ids(settings: Settings):
    assert forced_hits(settings.playbook_dir, []) == []


def test_forced_hits_ignore_an_unknown_id(settings: Settings):
    assert forced_hits(settings.playbook_dir, ["PB-NOPE-9"]) == []
