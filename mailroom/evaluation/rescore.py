"""Re-score an existing result file under the current gold and scoring rules.

A result file keeps every letter's prediction: the extracted fields, the queued
actions, the retrieved procedure ids and the terminal status. That is enough to
score the run again without touching the model, so a corrected gold label or a
changed scoring rule can be applied to a run that was already paid for.

Two rules hold here:

1. No model call. The scores are recomputed from the stored predictions.
2. The gold labels are read from `evals/golden` (or the set the run names), never
   from the copy the run wrote into itself. A rescore answers one question: what
   would this run have scored against today's labels?
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..config import DEFAULT_EVAL_SET, Settings
from . import metrics
from .harness import load_golden

# Per-letter keys the scorer cannot work without. A result file predating one of
# these cannot be rescored, and the caller is told which key is missing.
REQUIRED_KEYS = ("id", "predicted_fields", "retrieved", "gold_playbooks")


class RescoreError(RuntimeError):
    """The result file cannot be rescored, with the reason in the message."""


def rescored_path(result_path: Path) -> Path:
    """Where the re-scored copy is written: beside the original, marked."""
    path = Path(result_path)
    return path.with_name(f"{path.stem}-rescored.json")


def rescore_payload(payload: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Return a copy of `payload` re-scored against the current gold labels."""
    records = payload.get("per_letter")
    if not records:
        raise RescoreError("the result file has no per-letter records to re-score")
    for record in records:
        missing = [key for key in REQUIRED_KEYS if key not in record]
        if missing:
            raise RescoreError(
                f"letter {record.get('id', '?')!r} is missing {', '.join(missing)}; "
                "the file does not store enough to re-score it"
            )

    case_set = str(payload.get("set") or DEFAULT_EVAL_SET)
    cases_dir = settings.golden_dir if case_set == DEFAULT_EVAL_SET else settings.holdout_dir
    golden = {case.id: case for case in load_golden(cases_dir)}

    rescored: list[dict[str, Any]] = []
    unknown: list[str] = []
    for record in records:
        case = golden.get(str(record.get("id")))
        if case is None:
            unknown.append(str(record.get("id")))
            continue
        predicted = record.get("predicted_fields")
        fresh = dict(record)
        fresh["gold_fields"] = case.gold_fields
        fresh["gold_playbooks"] = case.gold_playbooks
        fresh["gold_action_kind"] = case.gold_action_kind
        fresh["gold_needs_human"] = case.gold_needs_human
        fresh["gold_injection_target"] = case.gold_injection_target
        fresh["field_hits"] = metrics.field_scores(case.gold_fields, predicted)
        fresh["predicted_is_suspicious"] = bool((predicted or {}).get("is_suspicious"))
        rescored.append(fresh)

    if unknown:
        raise RescoreError(
            "no current gold label for: " + ", ".join(sorted(unknown))
        )

    block = metrics.aggregate(rescored)
    block["action_kind_accuracy"] = metrics.action_kind_accuracy(rescored)

    result = dict(payload)
    result["per_letter"] = rescored
    result["metrics"] = block
    result["letters"] = len(rescored)
    return result


def rescore_result(settings: Settings, result_path: Path) -> Path:
    """Re-score one result file and write `<name>-rescored.json` next to it."""
    path = Path(result_path)
    if not path.exists():
        raise RescoreError(f"no such result file: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RescoreError(f"{path} is not valid JSON: {exc}") from exc

    rescored = rescore_payload(payload, settings)
    rescored["rescored_from"] = path.name
    # Model time is not stored by runs that predate per-letter model accounting,
    # and no model call is made here, so it cannot be recomputed. Say so in the
    # file rather than publishing a zero.
    if not any("model_latency_ms" in record for record in rescored["per_letter"]):
        rescored["rescored_note"] = (
            "model time is not stored in the source file, so the model-latency "
            "columns are absent from this re-score; the field, retrieval and "
            "safety numbers are recomputed"
        )
    out = rescored_path(path)
    out.write_text(json.dumps(rescored, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return out


__all__ = ["REQUIRED_KEYS", "RescoreError", "rescore_payload", "rescore_result", "rescored_path"]
