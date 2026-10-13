"""The audit trail: what gets written, and the run summary."""

from __future__ import annotations

import json
from pathlib import Path

from mailroom.audit import (
    AuditLog,
    input_hash,
    model_time_ms,
    new_run_id,
    read_run,
    summarize_run,
)


def test_run_ids_are_unique_and_file_safe():
    ids = {new_run_id() for _ in range(200)}
    assert len(ids) == 200
    assert all(len(value) == 12 and value.isalnum() for value in ids)


def test_input_hash_is_stable_and_short():
    assert input_hash("a letter") == input_hash("a letter")
    assert input_hash("a letter") != input_hash("another letter")
    assert len(input_hash("a letter")) == 16


def test_a_run_file_is_created_on_first_write(tmp_path: Path):
    log = AuditLog("run1", tmp_path / "runs")
    assert not log.path.exists()
    log.record("note", message="hello")
    assert log.path.exists()


def test_events_are_written_as_json_lines(tmp_path: Path):
    log = AuditLog("run1", tmp_path / "runs")
    log.record("note", message="one")
    log.record("note", message="two")
    events = read_run("run1", tmp_path / "runs")
    assert [event["message"] for event in events] == ["one", "two"]
    assert all(event["run_id"] == "run1" for event in events)
    assert all("timestamp" in event for event in events)


def test_a_model_call_records_tokens_latency_and_retry_count(tmp_path: Path):
    log = AuditLog("run1", tmp_path / "runs")
    log.model_call(
        model="command-a-plus-05-2026",
        attempt=3,
        latency_ms=812,
        status="ok",
        tokens_in=1400,
        tokens_out=155,
    )
    event = read_run("run1", tmp_path / "runs")[0]
    assert event["kind"] == "model_call"
    assert event["retry_count"] == 2
    assert event["tokens_in"] == 1400
    assert event["latency_ms"] == 812


def test_a_tool_call_records_args_output_and_guard(tmp_path: Path):
    log = AuditLog("run1", tmp_path / "runs")
    log.tool_call(
        step=2,
        tool="propose_action",
        args={"kind": "pay"},
        output={"blocked": True},
        latency_ms=4,
        guard={"allowed": False, "reasons": ["unknown payee"]},
    )
    event = read_run("run1", tmp_path / "runs")[0]
    assert event["tool"] == "propose_action"
    assert event["guard"]["allowed"] is False


def test_reading_an_unknown_run_returns_nothing(tmp_path: Path):
    assert read_run("nope", tmp_path / "runs") == []


def test_an_unparsable_line_is_kept_as_a_marker(tmp_path: Path):
    directory = tmp_path / "runs"
    directory.mkdir()
    (directory / "run1.jsonl").write_text('{"kind":"note"}\nnot json\n', encoding="utf-8")
    events = read_run("run1", directory)
    assert events[0]["kind"] == "note"
    assert events[1]["kind"] == "unparsable"


def test_non_ascii_survives_a_round_trip(tmp_path: Path):
    log = AuditLog("run1", tmp_path / "runs")
    log.record("note", message="Revenu Québec — mise en demeure")
    raw = log.path.read_text(encoding="utf-8")
    assert "Québec" in raw
    assert json.loads(raw.strip())["message"].endswith("mise en demeure")


def test_summary_folds_a_run_into_the_numbers_a_reader_wants(tmp_path: Path):
    log = AuditLog("run1", tmp_path / "runs")
    log.run_start(source="x", letter_hash="abc", backend="fake")
    log.model_call(model="m", attempt=1, latency_ms=10, status="error", error="429")
    log.model_call(model="m", attempt=2, latency_ms=20, status="ok", tokens_in=100, tokens_out=20)
    log.tool_call(step=1, tool="search_playbook", args={}, output={}, latency_ms=3)
    log.run_end(status="needs_human", reason="blocked", steps=4, latency_ms=900)
    summary = summarize_run(read_run("run1", tmp_path / "runs"))
    assert summary["model_calls"] == 1
    assert summary["model_failures"] == 1
    assert summary["retries"] == 1
    assert summary["tool_calls"] == 1
    assert summary["tokens_in"] == 100
    assert summary["status"] == "needs_human"
    assert summary["latency_ms"] == 900


def test_summarising_nothing_is_safe():
    summary = summarize_run([])
    assert summary["run_id"] is None
    assert summary["status"] is None
    assert summary["events"] == 0


# -- model time ------------------------------------------------------------


def test_a_model_call_records_its_operation(tmp_path: Path):
    log = AuditLog("run1", tmp_path / "runs")
    log.model_call(model="embed-v4.0", attempt=1, latency_ms=5, status="ok", operation="embed")
    assert read_run("run1", tmp_path / "runs")[0]["operation"] == "embed"


def test_a_model_call_defaults_to_the_chat_operation(tmp_path: Path):
    log = AuditLog("run1", tmp_path / "runs")
    log.model_call(model="m", attempt=1, latency_ms=5, status="ok")
    assert read_run("run1", tmp_path / "runs")[0]["operation"] == "chat"


def test_model_time_separates_chat_from_retrieval():
    events = [
        {"kind": "model_call", "operation": "chat", "latency_ms": 1000},
        {"kind": "model_call", "operation": "embed", "latency_ms": 300},
        {"kind": "model_call", "operation": "rerank", "latency_ms": 200},
        {"kind": "tool_call", "latency_ms": 75},
    ]
    assert model_time_ms(events) == {"model_latency_ms": 1500, "retrieval_latency_ms": 500}


def test_a_models_listing_is_not_letter_work():
    events = [{"kind": "model_call", "operation": "models", "latency_ms": 900}]
    assert model_time_ms(events) == {"model_latency_ms": 0, "retrieval_latency_ms": 0}


def test_model_time_counts_a_failed_attempt():
    events = [
        {"kind": "model_call", "operation": "chat", "latency_ms": 400, "status": "error"},
        {"kind": "model_call", "operation": "chat", "latency_ms": 600, "status": "ok"},
    ]
    assert model_time_ms(events)["model_latency_ms"] == 1000


def test_model_time_of_a_run_with_no_model_calls_is_zero():
    assert model_time_ms([{"kind": "note"}]) == {"model_latency_ms": 0, "retrieval_latency_ms": 0}


def test_the_summary_reports_model_and_retrieval_time(tmp_path: Path):
    log = AuditLog("run1", tmp_path / "runs")
    log.model_call(model="m", attempt=1, latency_ms=900, status="ok", tokens_in=10)
    log.model_call(model="e", attempt=1, latency_ms=120, status="ok", operation="embed")
    summary = summarize_run(read_run("run1", tmp_path / "runs"))
    assert summary["model_latency_ms"] == 1020
    assert summary["retrieval_latency_ms"] == 120
