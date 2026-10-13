"""Data model for the triage pipeline.

Two vocabularies live here and must not be confused:

* `LetterType` / `RequiredAction` describe the letter.
* `ActionKind` describes what the agent proposes to do about it. Every proposed
  write becomes a pending item in the queue; nothing executes on its own.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Language(StrEnum):
    FR = "fr"
    EN = "en"


class LetterType(StrEnum):
    """What the letter is. Ambiguous letters carry their best-fit type."""

    ROUTINE_NOTICE = "routine_notice"
    PAYMENT_DEMAND = "payment_demand"
    FINAL_NOTICE = "final_notice"
    REGISTRY_DECLARATION_REMINDER = "registry_declaration_reminder"
    INFORMATION_ONLY = "information_only"
    PAYROLL_REMITTANCE = "payroll_remittance"
    PHISHING = "phishing"


class RequiredAction(StrEnum):
    """What the letter asks for. `escalate` is the agent's "I cannot settle this"."""

    PAY = "pay"
    CONTEST = "contest"
    FILE_DECLARATION = "file_declaration"
    PROVIDE_DOCUMENTS = "provide_documents"
    CONFIRM_ADDRESS = "confirm_address"
    UPDATE_PAYROLL = "update_payroll"
    REPORT_PHISHING = "report_phishing"
    NO_ACTION = "no_action"
    ESCALATE = "escalate"


class ActionKind(StrEnum):
    """What the agent proposes to do. `pay`, `send`, `file` and `contest` write."""

    PAY = "pay"
    SEND = "send"
    FILE = "file"
    CONTEST = "contest"
    ESCALATE = "escalate"
    NO_ACTION = "no_action"


WRITE_KINDS: frozenset[ActionKind] = frozenset(
    {ActionKind.PAY, ActionKind.SEND, ActionKind.FILE, ActionKind.CONTEST}
)

# Kinds whose payload names a payee and an account, and so pass the payee guard.
PAYEE_KINDS: frozenset[ActionKind] = frozenset({ActionKind.PAY, ActionKind.SEND})

REQUIRED_ACTION_TO_KIND: dict[RequiredAction, ActionKind] = {
    RequiredAction.PAY: ActionKind.PAY,
    RequiredAction.CONTEST: ActionKind.CONTEST,
    RequiredAction.FILE_DECLARATION: ActionKind.FILE,
    RequiredAction.PROVIDE_DOCUMENTS: ActionKind.SEND,
    RequiredAction.CONFIRM_ADDRESS: ActionKind.SEND,
    RequiredAction.UPDATE_PAYROLL: ActionKind.FILE,
    RequiredAction.REPORT_PHISHING: ActionKind.ESCALATE,
    RequiredAction.NO_ACTION: ActionKind.NO_ACTION,
    RequiredAction.ESCALATE: ActionKind.ESCALATE,
}


class TriageStatus(StrEnum):
    """Terminal state of one triage run.

    FILED          -- information only, nothing to approve.
    ACTION_PENDING -- an action is queued and waits for a human.
    NEEDS_HUMAN    -- the agent will not settle this; a human must read it.
    ERROR          -- the run could not complete (no key, transport failure).
    """

    FILED = "filed"
    ACTION_PENDING = "action_pending"
    NEEDS_HUMAN = "needs_human"
    ERROR = "error"


class LetterFields(BaseModel):
    """The structured facts `extract_fields` must return."""

    model_config = ConfigDict(extra="forbid")

    issuer: str
    letter_type: LetterType
    language: Language
    reference_number: str | None = None
    amount_due: float | None = None
    currency: str | None = None
    due_date: date | None = None
    required_action: RequiredAction
    urgency: int = Field(ge=1, le=5)
    is_suspicious: bool = False
    suspicion_reason: str | None = None

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().upper()
        return cleaned or None


class RetrievalHit(BaseModel):
    """One playbook chunk as returned by the retrieval path."""

    playbook_id: str
    title: str
    chunk_id: str
    text: str
    owner: str = ""
    deadline_rule: str = ""
    score: float
    stage: Literal["vector", "rerank"]


class GuardVerdict(BaseModel):
    """Deterministic decision about one proposed action."""

    allowed: bool
    reasons: list[str] = Field(default_factory=list)

    def reason_text(self) -> str:
        return "; ".join(self.reasons) if self.reasons else "no objection"


class ProposedAction(BaseModel):
    """What the agent wants to do. Never executed by the agent."""

    kind: ActionKind
    payload: dict[str, Any] = Field(default_factory=dict)


class PendingAction(BaseModel):
    """A queued item. `status` moves only through approve/reject."""

    id: str
    run_id: str
    created_at: datetime
    kind: ActionKind
    payload: dict[str, Any] = Field(default_factory=dict)
    blocked: bool = False
    guard_reasons: list[str] = Field(default_factory=list)
    status: Literal["pending", "approved", "rejected"] = "pending"
    decided_at: datetime | None = None
    decision_note: str | None = None


class TriageResult(BaseModel):
    """Everything one triage run produced, minus the audit trail on disk."""

    run_id: str
    source: str | None = None
    status: TriageStatus
    reason: str
    fields: LetterFields | None = None
    retrieval: list[RetrievalHit] = Field(default_factory=list)
    actions: list[PendingAction] = Field(default_factory=list)
    blocked_actions: list[PendingAction] = Field(default_factory=list)
    injection_flags: list[str] = Field(default_factory=list)
    company_matches: list[dict[str, Any]] = Field(default_factory=list)
    plan: list[str] = Field(default_factory=list)
    steps: int = 0
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    notes: list[str] = Field(default_factory=list)


__all__ = [
    "PAYEE_KINDS",
    "REQUIRED_ACTION_TO_KIND",
    "WRITE_KINDS",
    "ActionKind",
    "GuardVerdict",
    "Language",
    "LetterFields",
    "LetterType",
    "PendingAction",
    "ProposedAction",
    "RequiredAction",
    "RetrievalHit",
    "TriageResult",
    "TriageStatus",
]
