"""Run the golden set through the pipeline and write a results file.

Two rules hold this together:

1. It refuses to run on the fake backend. Eval numbers come from Cohere or from
   nowhere. A rule-based stand-in would produce numbers that mean nothing.
2. It runs in a scratch state directory. The eval never writes to the real
   pending queue or to the real audit trail, so a bad run cannot approve
   anything or bury a real trace.
"""

from __future__ import annotations

import json
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from ..agent import Triager
from ..config import (
    DEFAULT_EVAL_SET,
    EVAL_CONFIGS,
    EVAL_SETS,
    EvalConfig,
    Settings,
    estimate_cost_usd,
)
from ..indexer import PlaybookIndex
from ..models import TriageResult, TriageStatus
from ..queue import ActionQueue
from ..registry import load_registry
from ..retrieval import Retriever, dedupe_by_playbook
from . import metrics

RESULTS_HEADER = "# Eval results"


@dataclass
class GoldenCase:
    id: str
    family: str
    language: str
    letter_text: str
    gold_fields: dict[str, Any]
    gold_playbooks: list[str] = field(default_factory=list)
    gold_action_kind: str | None = None
    gold_needs_human: bool = False
    gold_injection_target: dict[str, str] = field(default_factory=dict)
    path: str = ""


def load_golden(golden_dir: Path, limit: int | None = None) -> list[GoldenCase]:
    """Read every golden case, in filename order."""
    cases: list[GoldenCase] = []
    for path in sorted(Path(golden_dir).glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        gold = payload.get("gold") or {}
        cases.append(
            GoldenCase(
                id=payload.get("id", path.stem),
                family=payload.get("family", "unknown"),
                language=payload.get("language", gold.get("language", "en")),
                letter_text=payload.get("letter_text", ""),
                gold_fields=gold,
                gold_playbooks=list(payload.get("gold_playbooks") or []),
                gold_action_kind=payload.get("gold_action_kind"),
                gold_needs_human=bool(payload.get("gold_needs_human")),
                gold_injection_target=dict(payload.get("gold_injection_target") or {}),
                path=path.name,
            )
        )
    if limit is not None:
        cases = cases[:limit]
    return cases


class EvalHarness:
    """Drives one eval config over one letter set."""

    def __init__(
        self,
        *,
        settings: Settings,
        backend: Any,
        config: EvalConfig,
        progress: Callable[[str], None] | None = None,
        case_set: str = DEFAULT_EVAL_SET,
    ) -> None:
        if getattr(backend, "name", "") == "fake" or settings.is_fake:
            raise RuntimeError(
                "the eval refuses to run on the fake backend: its numbers would be meaningless. "
                "Set COHERE_API_KEY (or store it with `pass insert apis/cohere`) and run again."
            )
        if case_set not in EVAL_SETS:
            raise ValueError(f"unknown letter set '{case_set}'. Use one of: {', '.join(EVAL_SETS)}")
        self.settings = settings
        self.backend = backend
        self.config = config
        self.case_set = case_set
        self.progress = progress or (lambda _message: None)

    @property
    def cases_dir(self) -> Path:
        """The directory the letters are read from, chosen by `--set`."""
        return self.settings.golden_dir if self.case_set == "golden" else self.settings.holdout_dir

    def run(self, *, limit: int | None = None) -> dict[str, Any]:
        settings = self.settings
        cases = load_golden(self.cases_dir, limit=limit)
        if not cases:
            raise RuntimeError(f"no golden cases under {self.cases_dir}")

        index_meta = self._ensure_index()
        registry = load_registry(settings.registry_path)

        with tempfile.TemporaryDirectory(prefix="mailroom-eval-") as scratch:
            scratch_settings = Settings.build(
                root=settings.root,
                backend=settings.backend,
                state_dir=Path(scratch),
                index_dir=settings.index_dir,
                queue_path=Path(scratch) / "queue.jsonl",
                runs_dir=Path(scratch) / "runs",
                chat_model=self.config.chat_model,
                embed_model=self.config.embed_model,
                rerank_model=self.config.rerank_model,
                max_steps=settings.max_steps,
                max_retries=settings.max_retries,
                timeout_seconds=settings.timeout_seconds,
                requests_per_minute=settings.requests_per_minute,
                retrieval_top_k=settings.retrieval_top_k,
                final_top_k=settings.final_top_k,
            )
            queue = ActionQueue(scratch_settings.queue_path)
            retriever = Retriever(
                scratch_settings,
                self.backend,
                index=PlaybookIndex(scratch_settings, self.backend),
                use_rerank=self.config.rerank_model is not None,
            )
            triager = Triager(
                backend=self.backend,
                retriever=retriever,
                registry=registry,
                queue=queue,
                settings=scratch_settings,
                model=self.config.chat_model,
            )
            per_letter = [
                self._run_case(triager, case, index=number)
                for number, case in enumerate(cases, start=1)
            ]

        payload = {
            "config": self.config.name,
            "set": self.case_set,
            "run_date": date.today().isoformat(),
            "backend": getattr(self.backend, "name", "unknown"),
            "models": {
                "chat": self.config.chat_model,
                "embed": self.config.embed_model,
                "rerank": self.config.rerank_model,
            },
            "rate_limit_per_minute": settings.requests_per_minute,
            "retrieval_top_k": settings.retrieval_top_k,
            "final_top_k": settings.final_top_k,
            "limit": limit,
            "letters": len(per_letter),
            "index": index_meta,
            "metrics": _metrics_block(per_letter),
            "per_letter": per_letter,
        }
        return payload

    # -- steps -----------------------------------------------------------

    def _ensure_index(self) -> dict[str, Any]:
        index = PlaybookIndex(self.settings, self.backend)
        meta = index.read_meta()
        if meta is None or meta.get("embed_model") != self.config.embed_model:
            self.progress(f"building the playbook index with {self.config.embed_model}")
            meta = index.build(force=True, verbose=False)
        return meta

    def _run_case(self, triager: Triager, case: GoldenCase, *, index: int) -> dict[str, Any]:
        self.progress(f"[{index}] {case.id} ({case.family}, {case.language})")
        started = time.perf_counter()
        try:
            result = triager.run(case.letter_text, source=case.id)
        except Exception as exc:  # noqa: BLE001 - one bad letter must not end the run
            elapsed = int((time.perf_counter() - started) * 1000)
            result = TriageResult(
                run_id="unstarted",
                source=case.id,
                status=TriageStatus.ERROR,
                reason=f"{type(exc).__name__}: {exc}",
                latency_ms=elapsed,
            )
        return self._record(case, result)

    def _record(self, case: GoldenCase, result: TriageResult) -> dict[str, Any]:
        predicted_fields = result.fields.model_dump(mode="json") if result.fields else None
        retrieved = [hit.playbook_id for hit in dedupe_by_playbook(result.retrieval)]
        cost = estimate_cost_usd(
            self.config.chat_model, result.input_tokens, result.output_tokens
        )
        return {
            "id": case.id,
            "family": case.family,
            "language": case.language,
            "run_id": result.run_id,
            "status": result.status.value,
            "reason": result.reason,
            "predicted_fields": predicted_fields,
            "gold_fields": case.gold_fields,
            "field_hits": metrics.field_scores(case.gold_fields, predicted_fields),
            "predicted_is_suspicious": bool(
                predicted_fields.get("is_suspicious") if predicted_fields else False
            ),
            "gold_playbooks": case.gold_playbooks,
            "retrieved": retrieved,
            "gold_action_kind": case.gold_action_kind,
            "gold_needs_human": case.gold_needs_human,
            "gold_injection_target": case.gold_injection_target,
            "actions": [
                {
                    "id": action.id,
                    "kind": action.kind.value,
                    "payload": action.payload,
                    "blocked": action.blocked,
                    "guard_reasons": action.guard_reasons,
                    "status": action.status,
                }
                for action in result.actions
            ],
            "injection_flags": result.injection_flags,
            "steps": result.steps,
            "latency_ms": result.latency_ms,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "cost_usd": cost,
        }


def _metrics_block(per_letter: list[dict[str, Any]]) -> dict[str, Any]:
    block = metrics.aggregate(per_letter)
    block["action_kind_accuracy"] = metrics.action_kind_accuracy(per_letter)
    return block


# -- output ----------------------------------------------------------------


def results_path(settings: Settings, config: EvalConfig, run_date: str, case_set: str) -> Path:
    return settings.results_dir / _results_name(config.name, case_set, run_date)


def _results_name(config_name: str, case_set: str, run_date: str) -> str:
    """Golden results keep their old file name; other sets carry the set suffix."""
    suffix = "" if case_set == DEFAULT_EVAL_SET else f"-{case_set}"
    return f"{run_date}-{config_name}{suffix}.json"


def write_results(settings: Settings, payload: dict[str, Any]) -> Path:
    settings.results_dir.mkdir(parents=True, exist_ok=True)
    name = _results_name(
        str(payload["config"]), str(payload.get("set", DEFAULT_EVAL_SET)), str(payload["run_date"])
    )
    path = settings.results_dir / name
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def load_results(settings: Settings) -> list[dict[str, Any]]:
    """Every result file on disk, oldest first, one per config-and-set (newest wins)."""
    if not settings.results_dir.exists():
        return []
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for path in sorted(settings.results_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        key = (str(payload.get("config")), str(payload.get("set", DEFAULT_EVAL_SET)))
        latest[key] = payload
    return [latest[key] for key in sorted(latest)]


__all__ = [
    "EVAL_CONFIGS",
    "EvalHarness",
    "GoldenCase",
    "load_golden",
    "load_results",
    "results_path",
    "write_results",
]
