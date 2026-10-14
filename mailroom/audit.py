"""Append-only JSONL audit trail, one file per triage run.

Every model call and every tool call goes through `AuditLog.record`. The file is
the ground truth for `mailroom trace`, for the latency and token metrics in the
eval, and for any later question about why the agent did something.

A model call carries an `operation`: `chat`, `embed`, `rerank` or `models`. Chat
is the agent thinking; embed and rerank are retrieval. Both are logged, because
both consume the same rate-limit budget and both take time.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Operations that count as model work on a letter. `models.list` is a setup call
# that never sees a letter, so it is excluded.
MODEL_OPERATIONS = frozenset({"chat", "embed", "rerank"})
RETRIEVAL_OPERATIONS = frozenset({"embed", "rerank"})


def new_run_id() -> str:
    """Short, file-safe identifier for one run."""
    return uuid.uuid4().hex[:12]


def input_hash(text: str) -> str:
    """Stable short hash of an input, so a trail can be matched to a letter."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class AuditLog:
    """Writes events for one run to `<runs_dir>/<run_id>.jsonl`."""

    def __init__(self, run_id: str, runs_dir: Path) -> None:
        self.run_id = run_id
        self.runs_dir = Path(runs_dir)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.runs_dir / f"{run_id}.jsonl"

    def record(self, kind: str, **fields: Any) -> dict[str, Any]:
        event: dict[str, Any] = {"timestamp": _now(), "run_id": self.run_id, "kind": kind}
        event.update(fields)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        return event

    def run_start(self, *, source: str | None, letter_hash: str, backend: str) -> None:
        self.record("run_start", source=source, input_hash=letter_hash, backend=backend)

    def run_end(self, *, status: str, reason: str, steps: int, latency_ms: int) -> None:
        self.record("run_end", status=status, reason=reason, steps=steps, latency_ms=latency_ms)

    def model_call(
        self,
        *,
        model: str,
        attempt: int,
        latency_ms: int,
        status: str,
        tokens_in: int = 0,
        tokens_out: int = 0,
        input_hash_value: str | None = None,
        error: str | None = None,
        operation: str = "chat",
    ) -> None:
        self.record(
            "model_call",
            operation=operation,
            model=model,
            attempt=attempt,
            retry_count=attempt - 1,
            latency_ms=latency_ms,
            status=status,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            input_hash=input_hash_value,
            error=error,
        )

    def tool_call(
        self,
        *,
        step: int,
        tool: str,
        args: dict[str, Any],
        output: Any,
        latency_ms: int,
        guard: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        self.record(
            "tool_call",
            step=step,
            tool=tool,
            args=args,
            output=output,
            latency_ms=latency_ms,
            guard=guard,
            error=error,
        )

    def note(self, *, step: int, message: str, **fields: Any) -> None:
        self.record("note", step=step, message=message, **fields)


def read_run(run_id: str, runs_dir: Path) -> list[dict[str, Any]]:
    """Read one run's events in order. Returns [] when the run file is absent."""
    path = Path(runs_dir) / f"{run_id}.jsonl"
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            events.append({"kind": "unparsable", "raw": line})
    return events


def model_time_ms(events: list[dict[str, Any]]) -> dict[str, int]:
    """Split one run's model work into total and retrieval time, in milliseconds.

    The sum is over the `latency_ms` of the model calls themselves. The client-side
    rate limiter waits *before* the call is timed, so limiter waiting is excluded:
    this is the model's time, not the run's wall-clock time.

    Failed attempts count too. They cost the same request budget and the same wall
    clock, so leaving them out would understate the run.
    """
    model_ms = 0
    retrieval_ms = 0
    for event in events:
        if event.get("kind") != "model_call":
            continue
        operation = str(event.get("operation", "chat"))
        if operation not in MODEL_OPERATIONS:
            continue
        latency = int(event.get("latency_ms") or 0)
        model_ms += latency
        if operation in RETRIEVAL_OPERATIONS:
            retrieval_ms += latency
    return {"model_latency_ms": model_ms, "retrieval_latency_ms": retrieval_ms}


def failed_model_calls(events: list[dict[str, Any]]) -> int:
    """Count the model calls that failed for good, not the failed attempts.

    One call runs its attempts in place, so its attempts are logged as a run of
    `model_call` events with rising attempt numbers: 1, 2, 3. A new call starts
    again at attempt 1. An error event is therefore the last word on a call when
    the next `model_call` event starts a new call (attempt 1), or when no later
    `model_call` event exists; an error followed by a higher attempt is a retry
    that may still succeed. The count matters because a backend that gives up
    leaves the letter looking like a normal `needs_human` result, so the run
    status alone cannot show it.
    """
    calls = [event for event in events if event.get("kind") == "model_call"]
    failures = 0
    for index, event in enumerate(calls):
        if event.get("status") != "error":
            continue
        following = calls[index + 1] if index + 1 < len(calls) else None
        if following is None or int(following.get("attempt") or 0) == 1:
            failures += 1
    return failures


def summarize_run(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold a run's events into the numbers a reader wants first."""
    models = [e for e in events if e.get("kind") == "model_call"]
    ok_calls = [e for e in models if e.get("status") == "ok"]
    runs_end = [e for e in events if e.get("kind") == "run_end"]
    tools = [e for e in events if e.get("kind") == "tool_call"]
    timing = model_time_ms(events)
    return {
        "run_id": events[0].get("run_id") if events else None,
        "events": len(events),
        "model_calls": len(ok_calls),
        "model_failures": len(models) - len(ok_calls),
        "retries": sum(int(e.get("retry_count") or 0) for e in ok_calls),
        "tool_calls": len(tools),
        "tokens_in": sum(int(e.get("tokens_in") or 0) for e in ok_calls),
        "tokens_out": sum(int(e.get("tokens_out") or 0) for e in ok_calls),
        "model_latency_ms": timing["model_latency_ms"],
        "retrieval_latency_ms": timing["retrieval_latency_ms"],
        "status": runs_end[-1].get("status") if runs_end else None,
        "reason": runs_end[-1].get("reason") if runs_end else None,
        "latency_ms": runs_end[-1].get("latency_ms") if runs_end else None,
    }


__all__ = [
    "MODEL_OPERATIONS",
    "RETRIEVAL_OPERATIONS",
    "AuditLog",
    "failed_model_calls",
    "input_hash",
    "model_time_ms",
    "new_run_id",
    "read_run",
    "summarize_run",
]
