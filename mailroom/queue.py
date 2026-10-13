"""The pending-action queue.

The agent never performs a write. `propose_action` appends a pending item here,
and `mailroom approve` / `mailroom reject` are the only paths that move an item
out of `pending`. The file is append-only JSONL: each state change is a new line
carrying the whole item, and the newest line for an id wins. That keeps the
history and makes a torn write harmless.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .models import ActionKind, PendingAction


class QueueError(RuntimeError):
    """Raised when an id is unknown or already decided."""


def _now() -> datetime:
    return datetime.now(UTC)


def new_item_id() -> str:
    return "q_" + uuid.uuid4().hex[:10]


class ActionQueue:
    """Append-only pending-item queue backed by one JSONL file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    # -- writing ---------------------------------------------------------

    def _append(self, item: PendingAction) -> PendingAction:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(item.model_dump(mode="json"), ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        return item

    def add(
        self,
        *,
        run_id: str,
        kind: ActionKind,
        payload: dict,
        blocked: bool = False,
        guard_reasons: list[str] | None = None,
    ) -> PendingAction:
        item = PendingAction(
            id=new_item_id(),
            run_id=run_id,
            created_at=_now(),
            kind=kind,
            payload=payload,
            blocked=blocked,
            guard_reasons=list(guard_reasons or []),
        )
        return self._append(item)

    def _decide(self, item_id: str, status: str, note: str | None) -> PendingAction:
        current = self.get(item_id)
        if current is None:
            raise QueueError(f"no pending item with id {item_id}")
        if current.status != "pending":
            raise QueueError(f"item {item_id} is already {current.status}")
        decided = current.model_copy(
            update={"status": status, "decided_at": _now(), "decision_note": note}
        )
        return self._append(decided)

    def approve(self, item_id: str, note: str | None = None) -> PendingAction:
        """Approve an item. The effect is simulated and recorded, nothing is sent."""
        return self._decide(item_id, "approved", note)

    def reject(self, item_id: str, note: str | None = None) -> PendingAction:
        return self._decide(item_id, "rejected", note)

    # -- reading ---------------------------------------------------------

    def all_items(self) -> list[PendingAction]:
        """Every item, newest state per id, in creation order."""
        if not self.path.exists():
            return []
        latest: dict[str, PendingAction] = {}
        order: list[str] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = PendingAction.model_validate(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                continue
            if item.id not in latest:
                order.append(item.id)
            latest[item.id] = item
        return [latest[item_id] for item_id in order]

    def get(self, item_id: str) -> PendingAction | None:
        for item in self.all_items():
            if item.id == item_id:
                return item
        return None

    def pending(self) -> list[PendingAction]:
        return [item for item in self.all_items() if item.status == "pending"]

    def decided(self) -> list[PendingAction]:
        return [item for item in self.all_items() if item.status != "pending"]


__all__ = ["ActionQueue", "QueueError", "new_item_id"]
