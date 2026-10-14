"""Re-scoring a stored result under the current gold, with no model call."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mailroom.config import Settings
from mailroom.evaluation.harness import load_golden
from mailroom.evaluation.rescore import (
    RescoreError,
    rescore_payload,
    rescore_result,
    rescored_path,
)


def _case(settings: Settings, letter_id: str):
    return next(case for case in load_golden(settings.golden_dir) if case.id == letter_id)


def _record_for(settings: Settings, letter_id: str) -> dict:
    """A stored letter whose prediction matches the file's current gold."""
    case = _case(settings, letter_id)
    return {
        "id": letter_id,
        "family": case.family,
        "predicted_fields": dict(case.gold_fields),
        "retrieved": list(case.gold_playbooks),
        "gold_playbooks": ["PB-NOPE-001"],
        "gold_fields": {"issuer": "stale"},
        "field_hits": {"issuer": False},
        "gold_action_kind": None,
        "gold_needs_human": False,
        "gold_injection_target": {},
        "status": "filed",
        "actions": [],
        "predicted_is_suspicious": False,
        "latency_ms": 20,
        "input_tokens": 3,
        "output_tokens": 4,
        "cost_usd": None,
    }


def test_rescored_path_marks_the_copy():
    assert rescored_path(Path("evals/results/2026-10-13-A.json")) == Path(
        "evals/results/2026-10-13-A-rescored.json"
    )


def test_a_rescore_scores_against_the_current_gold(settings: Settings):
    payload = {"config": "A", "set": "golden", "per_letter": [_record_for(settings, "routine-07")]}
    rescored = rescore_payload(payload, settings)
    letter = rescored["per_letter"][0]
    assert all(letter["field_hits"].values())
    assert letter["gold_fields"] == _case(settings, "routine-07").gold_fields
    assert rescored["metrics"]["goal"]["all_fields_exact"] == 1.0


def test_the_stored_metrics_are_replaced(settings: Settings):
    payload = {
        "config": "A",
        "set": "golden",
        "metrics": {"goal": {"all_fields_exact": 0.0}},
        "per_letter": [_record_for(settings, "routine-07")],
    }
    rescored = rescore_payload(payload, settings)
    assert rescored["metrics"]["goal"]["all_fields_exact"] == 1.0


def test_a_result_without_predictions_cannot_be_rescored(settings: Settings):
    payload = {"per_letter": [{"id": "routine-07", "retrieved": [], "gold_playbooks": []}]}
    with pytest.raises(RescoreError, match="predicted_fields"):
        rescore_payload(payload, settings)


def test_a_file_with_no_letters_cannot_be_rescored(settings: Settings):
    with pytest.raises(RescoreError, match="no per-letter records"):
        rescore_payload({"per_letter": []}, settings)


def test_a_letter_with_no_current_gold_is_reported(settings: Settings):
    record = _record_for(settings, "routine-07")
    record["id"] = "no-such-letter"
    with pytest.raises(RescoreError, match="no current gold label"):
        rescore_payload({"per_letter": [record]}, settings)


def test_rescore_result_writes_beside_the_source(settings: Settings, tmp_path: Path):
    payload = {
        "config": "A",
        "set": "golden",
        "run_date": "2026-10-13",
        "letters": 1,
        "models": {"chat": "m", "embed": "e", "rerank": None},
        "per_letter": [_record_for(settings, "routine-07")],
    }
    source = tmp_path / "2026-10-13-A.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    out = rescore_result(settings, source)
    assert out == tmp_path / "2026-10-13-A-rescored.json"
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["rescored_from"] == "2026-10-13-A.json"
    assert written["metrics"]["goal"]["all_fields_exact"] == 1.0


def test_a_missing_file_is_reported(settings: Settings, tmp_path: Path):
    with pytest.raises(RescoreError, match="no such result file"):
        rescore_result(settings, tmp_path / "absent.json")


# -- failure classes are recounted from the kept audit trails ---------------


def test_rescore_recounts_a_422_as_a_rejection_not_a_backend_failure(tmp_path):
    import json as _json

    from mailroom.evaluation.rescore import _recount_failures

    traces = tmp_path / "eval-runs" / "2026-10-14-A-golden"
    traces.mkdir(parents=True)
    events = [
        {"kind": "model_call", "operation": "chat", "attempt": 1, "status": "ok"},
        {
            "kind": "model_call",
            "operation": "chat",
            "attempt": 1,
            "status": "error",
            "error": "UnprocessableEntityError: 422",
        },
    ]
    (traces / "ocr-03.jsonl").write_text(
        "\n".join(_json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )
    record = {"id": "ocr-03", "run_id": "ocr-03", "backend_failures": 1}
    _recount_failures(record, traces)
    assert record["backend_failures"] == 0
    assert record["model_rejections"] == 1


def test_rescore_leaves_counts_alone_when_the_trail_is_gone(tmp_path):
    from mailroom.evaluation.rescore import _recount_failures

    record = {"id": "x", "run_id": "x", "backend_failures": 1}
    _recount_failures(record, tmp_path)
    assert record == {"id": "x", "run_id": "x", "backend_failures": 1}
    _recount_failures(record, None)
    assert record["backend_failures"] == 1
