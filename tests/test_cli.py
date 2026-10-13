"""The command line, driven end to end on the offline backend."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mailroom.cli import app
from mailroom.config import REPO_ROOT

runner = CliRunner()
INJECTION = REPO_ROOT / "evals" / "golden" / "injection-01.json"


def combined(result) -> str:
    return (result.output or "") + (getattr(result, "stderr", "") or "")


def run(tmp_path: Path, *args: str):
    return runner.invoke(
        app,
        [
            "--backend",
            "fake",
            "--root",
            str(REPO_ROOT),
            "--state-dir",
            str(tmp_path / "state"),
            *args,
        ],
    )


def test_help_lists_every_command():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("index", "triage", "trace", "queue", "approve", "reject", "eval", "models"):
        assert command in result.output


def test_index_builds_the_index(tmp_path: Path):
    result = run(tmp_path, "index")
    assert result.exit_code == 0
    assert "indexed" in result.output
    assert "procedures" in result.output


def test_index_reports_an_existing_index(tmp_path: Path):
    run(tmp_path, "index")
    result = run(tmp_path, "index")
    assert "already built" in result.output


def test_index_force_rebuilds(tmp_path: Path):
    run(tmp_path, "index")
    result = run(tmp_path, "index", "--force")
    assert "indexed" in result.output


def test_triage_of_an_injection_case_shows_a_blocked_action_and_a_run_id(tmp_path: Path):
    run(tmp_path, "index")
    result = run(tmp_path, "triage", str(INJECTION), "--text-only")
    assert result.exit_code == 0
    assert "run id:" in result.output
    assert "needs_human" in result.output
    assert "BLOCKED" in result.output
    assert "injection wording" in result.output


def test_triage_without_text_only_prints_json(tmp_path: Path):
    run(tmp_path, "index")
    result = run(tmp_path, "triage", str(INJECTION))
    payload = json.loads(result.output.split("\n\n")[0])
    assert payload["status"] == "needs_human"
    assert payload["injection_flags"]


def run_id_from(output: str) -> str:
    for line in output.splitlines():
        if line.startswith("run id:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"no run id in output: {output}")


def test_trace_replays_the_run(tmp_path: Path):
    run(tmp_path, "index")
    triage = run(tmp_path, "triage", str(INJECTION), "--text-only")
    run_id = run_id_from(triage.output)
    result = run(tmp_path, "trace", run_id)
    assert result.exit_code == 0
    assert "model_call" in result.output
    assert "tool_call" in result.output
    assert "run_end" in result.output


def test_trace_of_an_unknown_run_fails(tmp_path: Path):
    result = run(tmp_path, "trace", "doesnotexist")
    assert result.exit_code == 2
    assert "No run" in combined(result)


def test_an_empty_queue_says_so(tmp_path: Path):
    result = run(tmp_path, "queue")
    assert "nothing pending" in result.output


def test_a_triaged_letter_appears_in_the_queue(tmp_path: Path):
    run(tmp_path, "index")
    run(tmp_path, "triage", str(INJECTION))
    result = run(tmp_path, "queue")
    assert "q_" in result.output


def test_approve_then_approve_again_is_refused(tmp_path: Path):
    run(tmp_path, "index")
    run(tmp_path, "triage", str(INJECTION))
    pending = run(tmp_path, "queue").output
    item_id = next(token for token in pending.split() if token.startswith("q_"))
    first = run(tmp_path, "approve", item_id, "--note", "checked by hand")
    assert first.exit_code == 0
    assert "approved" in first.output
    assert "simulated" in first.output
    second = run(tmp_path, "approve", item_id)
    assert second.exit_code == 2
    assert "already approved" in combined(second)


def test_reject_marks_the_item_rejected(tmp_path: Path):
    run(tmp_path, "index")
    run(tmp_path, "triage", str(INJECTION))
    pending = run(tmp_path, "queue").output
    item_id = next(token for token in pending.split() if token.startswith("q_"))
    result = run(tmp_path, "reject", item_id)
    assert result.exit_code == 0
    assert "rejected" in result.output


def test_queue_all_shows_decided_items(tmp_path: Path):
    run(tmp_path, "index")
    run(tmp_path, "triage", str(INJECTION))
    pending = run(tmp_path, "queue").output
    item_id = next(token for token in pending.split() if token.startswith("q_"))
    run(tmp_path, "approve", item_id)
    assert "nothing pending" in run(tmp_path, "queue").output
    assert item_id in run(tmp_path, "queue", "--all").output


def test_approving_an_unknown_id_fails(tmp_path: Path):
    result = run(tmp_path, "approve", "q_nope")
    assert result.exit_code == 2


def test_triage_of_a_missing_file_fails(tmp_path: Path):
    result = run(tmp_path, "triage", str(tmp_path / "absent.txt"))
    assert result.exit_code == 2
    assert "No such file" in combined(result)


def test_triage_reads_a_plain_text_letter(tmp_path: Path):
    run(tmp_path, "index")
    letter = tmp_path / "letter.txt"
    letter.write_text(
        "Ville de Laval\nService de la taxation\n\nExemple Toiture SENC\n\n"
        "Le montant de 100,00 $ CAD est dû.\nCompte : 1199999003-LAV\n"
        "Date limite : 2 novembre 2026\n",
        encoding="utf-8",
    )
    result = run(tmp_path, "triage", str(letter), "--text-only")
    assert result.exit_code == 0
    assert "Ville de Laval" in result.output


def test_render_only_writes_the_not_yet_run_doc(tmp_path: Path):
    root = tmp_path / "repo"
    (root / "evals" / "results").mkdir(parents=True)
    result = runner.invoke(
        app, ["--backend", "fake", "--root", str(root), "eval", "--render-only"]
    )
    assert result.exit_code == 0
    text = (root / "docs" / "EVALS.md").read_text(encoding="utf-8")
    assert "Not yet run" in text


def test_eval_refuses_to_run_on_the_fake_backend(tmp_path: Path):
    result = run(tmp_path, "eval", "--config", "A", "--limit", "1")
    assert result.exit_code == 2
    assert "refuses to run on the fake backend" in combined(result)


def test_eval_rejects_an_unknown_config(tmp_path: Path):
    result = run(tmp_path, "eval", "--config", "Z")
    assert result.exit_code == 2
    assert "Unknown config" in combined(result)


def test_eval_rejects_an_unknown_letter_set(tmp_path: Path):
    result = run(tmp_path, "eval", "--set", "mystery")
    assert result.exit_code == 2
    assert "Unknown letter set" in combined(result)


def test_models_without_a_key_says_so_and_writes_the_doc(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    result = runner.invoke(
        app, ["--backend", "fake", "--root", str(root), "models", "--write-docs"]
    )
    assert result.exit_code == 0
    assert "No API key" in result.output
    text = (root / "docs" / "MODELS.md").read_text(encoding="utf-8")
    assert "Not recorded" in text
    assert "command-a-plus-05-2026" in text


@pytest.mark.parametrize("command", ["index", "triage", "eval"])
def test_commands_report_a_missing_key_without_the_fake_backend(tmp_path: Path, command: str):
    args = [command, str(INJECTION)] if command == "triage" else [command]
    result = runner.invoke(
        app,
        ["--root", str(REPO_ROOT), "--state-dir", str(tmp_path / "state"), *args],
        env={"COHERE_API_KEY": "", "PATH": "/nonexistent"},
    )
    assert result.exit_code == 2
    assert "No Cohere API key" in combined(result)
