"""Result files, the EVALS.md generator and the harness guard."""

from __future__ import annotations

from pathlib import Path

import pytest

from mailroom.config import EVAL_CONFIGS, Settings
from mailroom.evaluation import report
from mailroom.evaluation.harness import (
    EvalHarness,
    GoldenCase,
    load_golden,
    load_results,
    write_results,
)
from mailroom.fake import FakeBackend


def _payload(config: str = "A", letters: int = 2) -> dict:
    per_letter = [
        {
            "id": f"x-{index}",
            "family": "payment",
            "language": "en",
            "status": "action_pending",
            "field_hits": {"letter_type": True, "urgency": index == 0},
            "gold_playbooks": ["PB-TAX-002"],
            "retrieved": ["PB-TAX-002", "PB-TAX-001"],
            "gold_action_kind": "pay",
            "gold_needs_human": False,
            "gold_injection_target": {},
            "actions": [{"kind": "pay", "status": "pending", "blocked": False, "payload": {}}],
            "latency_ms": 500 + index,
            "input_tokens": 100,
            "output_tokens": 20,
            "cost_usd": None,
        }
        for index in range(letters)
    ]
    from mailroom.evaluation import metrics

    block = metrics.aggregate(per_letter)
    block["action_kind_accuracy"] = metrics.action_kind_accuracy(per_letter)
    return {
        "config": config,
        "set": "golden",
        "run_date": "2026-10-13",
        "backend": "cohere",
        "models": {
            "chat": "command-a-plus-05-2026",
            "embed": "embed-v4.0",
            "rerank": "rerank-v4.0-pro",
        },
        "rate_limit_per_minute": 15,
        "retrieval_top_k": 20,
        "final_top_k": 3,
        "limit": None,
        "letters": letters,
        "index": {"chunks": 40},
        "metrics": block,
        "per_letter": per_letter,
    }


def test_the_doc_says_plainly_that_nothing_has_run_yet():
    text = report.render_evals_doc([])
    assert "Not yet run" in text
    assert "COHERE_API_KEY" in text
    assert "| Config |" not in text


def test_the_doc_lists_a_result_when_one_exists():
    text = report.render_evals_doc([_payload()])
    assert "Not yet run" not in text
    assert "| Config | Chat model |" in text
    assert "command-a-plus-05-2026" in text


def test_the_results_table_has_one_row_per_config():
    table = report.results_table([_payload("A"), _payload("B"), _payload("C")])
    lines = table.strip().splitlines()
    assert len(lines) == 5  # header, separator, three rows
    for name in ("A", "B", "C"):
        assert f"| {name} |" in table


def test_cost_is_reported_as_not_available_when_no_price_is_configured():
    assert "n/a" in report.results_table([_payload()])
    assert "PRICES_PER_MILLION" in report.render_evals_doc([_payload()])


def test_the_readme_table_says_so_when_there_is_no_run():
    assert "No eval run yet" in report.readme_table([])


def test_the_readme_table_is_the_same_table_as_the_doc():
    payload = _payload()
    assert report.readme_table([payload]) == report.results_table([payload])


def test_the_field_table_lists_every_scored_field():
    table = report.field_table(_payload())
    from mailroom.evaluation.metrics import SCORED_FIELDS

    for field in SCORED_FIELDS:
        assert f"| {field} |" in table


def test_the_family_table_groups_by_family():
    table = report.family_table(_payload())
    assert "| payment | 2 |" in table


def test_writing_the_doc_creates_the_file(tmp_path: Path):
    path = report.write_evals_doc(tmp_path, [_payload()])
    assert path.exists()
    assert "Eval results" in path.read_text(encoding="utf-8")


def test_results_round_trip_through_the_results_directory(tmp_path: Path):
    settings = Settings.build(root=tmp_path, backend="cohere")
    settings.results_dir.mkdir(parents=True, exist_ok=True)
    written = write_results(settings, _payload("A"))
    assert written.exists()
    loaded = load_results(settings)
    assert len(loaded) == 1
    assert loaded[0]["config"] == "A"


def test_the_newest_result_per_config_wins(tmp_path: Path):
    settings = Settings.build(root=tmp_path, backend="cohere")
    settings.results_dir.mkdir(parents=True, exist_ok=True)
    older = _payload("A")
    older["run_date"] = "2026-10-01"
    newer = _payload("A")
    newer["run_date"] = "2026-10-13"
    write_results(settings, older)
    write_results(settings, newer)
    assert [item["run_date"] for item in load_results(settings)] == ["2026-10-13"]


def test_a_golden_result_keeps_its_plain_file_name(tmp_path: Path):
    settings = Settings.build(root=tmp_path, backend="cohere")
    written = write_results(settings, _payload("A"))
    assert written.name == "2026-10-13-A.json"


def test_a_holdout_result_is_named_with_the_set_suffix(tmp_path: Path):
    settings = Settings.build(root=tmp_path, backend="cohere")
    payload = _payload("A")
    payload["set"] = "holdout"
    written = write_results(settings, payload)
    assert written.name == "2026-10-13-A-holdout.json"


def test_results_are_kept_apart_by_letter_set(tmp_path: Path):
    settings = Settings.build(root=tmp_path, backend="cohere")
    holdout = _payload("A")
    holdout["set"] = "holdout"
    write_results(settings, _payload("A"))
    write_results(settings, holdout)
    loaded = load_results(settings)
    assert [(item["config"], item["set"]) for item in loaded] == [("A", "golden"), ("A", "holdout")]


def test_the_results_table_names_the_letter_set():
    table = report.results_table([_payload()])
    assert "| Set |" in table
    assert "| golden |" in table


def test_every_result_column_has_a_header():
    header = report.results_table([_payload()]).splitlines()[0]
    assert header.count("|") == len(report.RESULT_COLUMNS) + 1


def test_the_harness_reads_the_holdout_directory_when_asked(repo_root: Path):
    live = Settings.build(root=repo_root, backend="cohere")
    harness = EvalHarness(
        settings=live, backend=_LiveBackend(), config=EVAL_CONFIGS["A"], case_set="holdout"
    )
    assert harness.cases_dir == live.holdout_dir
    assert harness.case_set == "holdout"


def test_the_harness_defaults_to_the_golden_set(repo_root: Path):
    live = Settings.build(root=repo_root, backend="cohere")
    harness = EvalHarness(settings=live, backend=_LiveBackend(), config=EVAL_CONFIGS["A"])
    assert harness.cases_dir == live.golden_dir


def test_the_harness_refuses_an_unknown_letter_set(repo_root: Path):
    live = Settings.build(root=repo_root, backend="cohere")
    with pytest.raises(ValueError, match="unknown letter set"):
        EvalHarness(
            settings=live, backend=_LiveBackend(), config=EVAL_CONFIGS["A"], case_set="mystery"
        )


def test_a_broken_result_file_is_skipped(tmp_path: Path):
    settings = Settings.build(root=tmp_path, backend="cohere")
    settings.results_dir.mkdir(parents=True, exist_ok=True)
    (settings.results_dir / "broken.json").write_text("{not json", encoding="utf-8")
    assert load_results(settings) == []


def test_the_harness_refuses_the_fake_backend(settings: Settings):
    with pytest.raises(RuntimeError, match="refuses to run on the fake backend"):
        EvalHarness(settings=settings, backend=FakeBackend(), config=EVAL_CONFIGS["A"])


def test_the_harness_refuses_a_fake_settings_backend(repo_root: Path):
    live = Settings.build(root=repo_root, backend="cohere")
    with pytest.raises(RuntimeError, match="refuses to run"):
        EvalHarness(settings=live, backend=_NamedBackend(), config=EVAL_CONFIGS["A"])


class _NamedBackend:
    name = "fake"


class _LiveBackend:
    """A backend the harness accepts, used to inspect configuration only."""

    name = "cohere"


def test_the_golden_loader_reads_gold_playbooks_and_targets(repo_root: Path):
    cases = load_golden(repo_root / "evals" / "golden")
    injection = next(case for case in cases if case.family == "injection")
    assert isinstance(injection, GoldenCase)
    assert injection.gold_injection_target
    assert injection.gold_playbooks
