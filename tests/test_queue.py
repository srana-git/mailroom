"""The pending queue: append-only state, approve and reject."""

from __future__ import annotations

from pathlib import Path

import pytest

from mailroom.models import ActionKind
from mailroom.queue import ActionQueue, QueueError, new_item_id


def _queue(tmp_path: Path) -> ActionQueue:
    return ActionQueue(tmp_path / "queue.jsonl")


def test_an_empty_queue_is_empty(tmp_path: Path):
    store = _queue(tmp_path)
    assert store.all_items() == []
    assert store.pending() == []


def test_adding_an_item_makes_it_pending(tmp_path: Path):
    store = _queue(tmp_path)
    item = store.add(run_id="r1", kind=ActionKind.PAY, payload={"amount": 10})
    assert item.status == "pending"
    assert item.id in {entry.id for entry in store.pending()}


def test_item_ids_are_unique():
    assert len({new_item_id() for _ in range(200)}) == 200


def test_approve_records_the_decision(tmp_path: Path):
    store = _queue(tmp_path)
    item = store.add(run_id="r1", kind=ActionKind.FILE, payload={})
    decided = store.approve(item.id, note="looks right")
    assert decided.status == "approved"
    assert decided.decision_note == "looks right"
    assert decided.decided_at is not None


def test_reject_records_the_decision(tmp_path: Path):
    store = _queue(tmp_path)
    item = store.add(run_id="r1", kind=ActionKind.SEND, payload={})
    assert store.reject(item.id).status == "rejected"


def test_a_decided_item_leaves_the_pending_list(tmp_path: Path):
    store = _queue(tmp_path)
    item = store.add(run_id="r1", kind=ActionKind.PAY, payload={})
    store.approve(item.id)
    assert store.pending() == []
    assert [entry.id for entry in store.decided()] == [item.id]


def test_deciding_twice_is_refused(tmp_path: Path):
    store = _queue(tmp_path)
    item = store.add(run_id="r1", kind=ActionKind.PAY, payload={})
    store.approve(item.id)
    with pytest.raises(QueueError, match="already approved"):
        store.reject(item.id)


def test_deciding_an_unknown_id_is_refused(tmp_path: Path):
    store = _queue(tmp_path)
    with pytest.raises(QueueError, match="no pending item"):
        store.approve("q_missing")


def test_the_file_is_append_only(tmp_path: Path):
    path = tmp_path / "queue.jsonl"
    store = ActionQueue(path)
    item = store.add(run_id="r1", kind=ActionKind.PAY, payload={})
    store.approve(item.id)
    assert len(path.read_text(encoding="utf-8").strip().splitlines()) == 2


def test_the_latest_line_for_an_id_wins(tmp_path: Path):
    store = _queue(tmp_path)
    item = store.add(run_id="r1", kind=ActionKind.PAY, payload={})
    store.approve(item.id)
    store2 = _queue(tmp_path)
    assert store2.get(item.id).status == "approved"


def test_blocked_items_keep_their_guard_reasons(tmp_path: Path):
    store = _queue(tmp_path)
    item = store.add(
        run_id="r1",
        kind=ActionKind.PAY,
        payload={"account": "8842119-447"},
        blocked=True,
        guard_reasons=["not a registry payee"],
    )
    assert item.blocked is True
    assert item.guard_reasons == ["not a registry payee"]


def test_a_corrupt_line_does_not_hide_the_good_ones(tmp_path: Path):
    path = tmp_path / "queue.jsonl"
    store = ActionQueue(path)
    item = store.add(run_id="r1", kind=ActionKind.PAY, payload={})
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    assert [entry.id for entry in store.all_items()] == [item.id]


def test_reading_a_missing_file_returns_nothing(tmp_path: Path):
    store = ActionQueue(tmp_path / "never-written.jsonl")
    assert store.all_items() == []
    assert store.get("q_x") is None
