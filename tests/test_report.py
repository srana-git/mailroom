"""Result files, the EVALS.md generator and the harness guard."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mailroom.config import EVAL_CONFIGS, REPO_ROOT, Settings
from mailroom.evaluation import report
from mailroom.evaluation.harness import (
    EvalHarness,
    GoldenCase,
    load_golden,
    load_results,
    write_results,
)
from mailroom.fake import FakeBackend
from mailroom.llm import BackendError


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
            "model_latency_ms": 120 + index,
            "retrieval_latency_ms": 30,
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


def test_the_results_table_reports_model_time_beside_wall_time():
    table = report.results_table([_payload()])
    assert "| Model p50 ms |" in table
    assert "| Model p95 ms |" in table
    assert "| 120 |" in table


def test_the_results_table_reports_the_core_fields_rate():
    table = report.results_table([_payload()])
    assert "| Core fields exact |" in table
    assert "| 0.0% |" in table


def test_the_results_table_has_a_backend_failures_column_after_letters():
    header = report.results_table([_payload()]).splitlines()[0]
    columns = [cell.strip() for cell in header.strip("|").split("|")]
    assert columns[columns.index("Letters") + 1] == "Backend failures"


def test_the_results_table_shows_the_backend_failure_count():
    payload = _payload(letters=3)
    payload["metrics"]["backend_failed_letters"] = 2
    row = report.results_table([payload]).splitlines()[2]
    cells = [cell.strip() for cell in row.strip("|").split("|")]
    assert cells[report.RESULT_COLUMNS.index("Backend failures")] == "2"


def test_the_results_table_reads_backend_failures_as_a_dash_when_the_run_predates_it():
    payload = _payload()
    del payload["metrics"]["backend_failed_letters"]
    row = report.results_table([payload]).splitlines()[2]
    cells = [cell.strip() for cell in row.strip("|").split("|")]
    assert cells[report.RESULT_COLUMNS.index("Backend failures")] == "-"


def test_a_run_with_backend_failures_is_marked_not_a_valid_measurement():
    payload = _payload()
    payload["metrics"]["backend_failed_letters"] = 1
    text = report.render_evals_doc([payload])
    assert "**Not a valid measurement.** 1 of 2 letters" in text
    assert "measure that failure, not the agent" in text


def test_a_run_with_rejected_requests_names_them_without_invalidating_it():
    payload = _payload()
    payload["metrics"]["model_rejected_letters"] = 1
    text = report.render_evals_doc([payload])
    assert (
        "1 of 2 letters ended because the API rejected the agent's request or the model's "
        "tool call (HTTP 400 or 422). The agent failed closed to a person. These are agent "
        "errors and count in the scores."
    ) in text
    assert "Not a valid measurement" not in text


def test_a_run_with_no_rejections_carries_no_rejection_sentence():
    text = report.render_evals_doc([_payload()])
    assert "ended because the API rejected" not in text


def test_a_clean_run_carries_no_validity_warning():
    assert "Not a valid measurement" not in report.render_evals_doc([_payload()])


def test_the_doc_lists_backend_failures_under_what_is_measured():
    text = report.render_evals_doc([_payload()])
    assert "Validity" in text
    assert "letters where a model call failed for good because of quota," in text
    assert "rate limit, auth, network or a server error" in text


def test_the_doc_lists_model_rejections_under_what_is_measured():
    text = report.render_evals_doc([_payload()])
    assert "model rejections: letters where the API rejected the agent's own request" in text
    assert "they do not make a run invalid" in text


def test_the_doc_explains_the_issuer_and_core_field_rules():
    text = report.render_evals_doc([_payload()])
    assert "## Scoring rules" in text
    assert "scored by organisation" in text
    assert "core fields exact" in text


def test_the_core_fields_rate_reads_as_not_available_when_the_run_predates_it():
    payload = _payload()
    del payload["metrics"]["goal"]["core_fields_exact"]
    text = report.render_evals_doc([payload])
    assert "predates the metric" in text


def test_model_time_reads_as_not_available_when_the_run_predates_it():
    payload = _payload()
    for record in payload["per_letter"]:
        del record["model_latency_ms"]
    assert "| n/a |" in report.results_table([payload])


def test_the_harness_keeps_its_traces_under_eval_runs(tmp_path: Path):
    from datetime import date

    live = Settings.build(root=tmp_path, backend="cohere", state_dir=tmp_path / "state")
    harness = EvalHarness(
        settings=live, backend=_LiveBackend(), config=EVAL_CONFIGS["A"], case_set="holdout"
    )
    expected = (
        tmp_path / "state" / "eval-runs" / f"{date.today().isoformat()}-A-holdout"
    )
    assert harness.traces_dir == expected


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


class _DownBackend:
    """A backend the harness accepts whose every chat call fails for good.

    It records the failed attempts itself, the way `CohereBackend._with_retries`
    does, so the audit trail shows a call that gave up after its retries.
    """

    name = "cohere"
    max_attempts = 4

    def __init__(self, audit=None) -> None:
        self.audit = audit

    def with_audit(self, audit):
        return _DownBackend(audit)

    def chat(self, *, model, messages, tools=None, response_format=None, temperature=0.0):
        if self.audit is not None:
            for attempt in range(1, self.max_attempts + 1):
                self.audit.model_call(
                    model=model,
                    attempt=attempt,
                    latency_ms=1,
                    status="error",
                    error="TooManyRequestsError: 429",
                )
        raise BackendError(
            f"chat failed after {self.max_attempts} attempt(s) on {model}: 429",
            attempts=self.max_attempts,
        )


def _down_backend_settings(tmp_path: Path) -> Settings:
    """Settings over three tiny cases, with index metadata so nothing is embedded."""
    golden = tmp_path / "golden"
    golden.mkdir()
    for number in range(3):
        (golden / f"case-{number}.json").write_text(
            json.dumps(
                {"id": f"case-{number}", "family": "routine", "letter_text": "Pay this invoice."}
            ),
            encoding="utf-8",
        )
    settings = Settings.build(
        root=REPO_ROOT,
        backend="cohere",
        state_dir=tmp_path / "state",
        index_dir=tmp_path / "state" / "index",
        queue_path=tmp_path / "state" / "queue.jsonl",
        golden_dir=golden,
        results_dir=tmp_path / "results",
    )
    settings.index_dir.mkdir(parents=True, exist_ok=True)
    (settings.index_dir / "meta.json").write_text(
        json.dumps({"embed_model": settings.embed_model}), encoding="utf-8"
    )
    return settings


def test_the_harness_stops_when_three_letters_in_a_row_lose_their_model_call(tmp_path: Path):
    settings = _down_backend_settings(tmp_path)
    backend = _DownBackend()
    harness = EvalHarness(settings=settings, backend=backend, config=EVAL_CONFIGS["A"])
    with pytest.raises(RuntimeError, match="in a row") as excinfo:
        harness.run()
    # The abort names the error the backend saw, not the agent layer's wrapper
    # around it, so a reader can tell a quota from a bad request.
    message = str(excinfo.value)
    assert "TooManyRequestsError: 429" in message
    assert "chat failed after" not in message
    assert not list(settings.results_dir.glob("*.json"))


def test_the_golden_loader_reads_gold_playbooks_and_targets(repo_root: Path):
    cases = load_golden(repo_root / "evals" / "golden")
    injection = next(case for case in cases if case.family == "injection")
    assert isinstance(injection, GoldenCase)
    assert injection.gold_injection_target
    assert injection.gold_playbooks
