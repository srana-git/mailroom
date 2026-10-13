"""Tool definitions handed to Command, and the local code that runs them.

Four tools, no more:

* `extract_fields`  the structured reading of the letter
* `search_playbook` the procedure that fits
* `lookup_company`  which of our companies the letter is about
* `propose_action`  the next move, queued for a human, never executed

Every argument object is validated by pydantic before it touches any local code.
A validation failure comes back to the model as a tool error so it can correct
itself once; the agent decides what happens after that.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .guards import check_action
from .models import (
    ActionKind,
    GuardVerdict,
    LetterFields,
    LetterType,
    RequiredAction,
)
from .queue import ActionQueue
from .registry import Registry
from .retrieval import Retriever

EXTRACT_FIELDS = "extract_fields"
SEARCH_PLAYBOOK = "search_playbook"
LOOKUP_COMPANY = "lookup_company"
PROPOSE_ACTION = "propose_action"

TOOL_NAMES = (EXTRACT_FIELDS, SEARCH_PLAYBOOK, LOOKUP_COMPANY, PROPOSE_ACTION)

_NULLABLE_STRING = {"type": ["string", "null"]}

EXTRACT_FIELDS_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": EXTRACT_FIELDS,
        "description": (
            "Record your reading of the letter. Use null for anything the letter does "
            "not state. Never invent a reference number, an amount or a date."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "issuer": {
                    "type": "string",
                    "description": "Who sent the letter, as printed on it.",
                },
                "letter_type": {
                    "type": "string",
                    "enum": [item.value for item in LetterType],
                    "description": "The best fit for what this letter is.",
                },
                "language": {
                    "type": "string",
                    "enum": ["fr", "en"],
                    "description": "Language the letter is written in.",
                },
                "reference_number": dict(
                    _NULLABLE_STRING,
                    description="Reference, file or account number printed on the letter.",
                ),
                "amount_due": {
                    "type": ["number", "null"],
                    "description": "Amount the letter asks for, as a number, no symbols.",
                },
                "currency": dict(
                    _NULLABLE_STRING,
                    description="ISO 4217 code, for example CAD, USD, EUR.",
                ),
                "due_date": dict(
                    _NULLABLE_STRING,
                    description="Deadline in ISO 8601 form, YYYY-MM-DD. Null when none is stated.",
                ),
                "required_action": {
                    "type": "string",
                    "enum": [item.value for item in RequiredAction],
                    "description": (
                        "What the letter asks for. Use 'escalate' when the letter does not "
                        "settle what must be done."
                    ),
                },
                "urgency": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5,
                    "description": "1 is filing, 5 is act today.",
                },
                "is_suspicious": {
                    "type": "boolean",
                    "description": "True when the letter looks forged, spoofed or fraudulent.",
                },
                "suspicion_reason": dict(
                    _NULLABLE_STRING,
                    description="One short sentence naming the doubt. Null when not suspicious.",
                ),
            },
            "required": [
                "issuer",
                "letter_type",
                "language",
                "reference_number",
                "amount_due",
                "currency",
                "due_date",
                "required_action",
                "urgency",
                "is_suspicious",
                "suspicion_reason",
            ],
        },
    },
}

SEARCH_PLAYBOOK_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": SEARCH_PLAYBOOK,
        "description": (
            "Find the internal procedures that fit this letter. Search with the situation, "
            "not with a procedure id."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to search for, in a few words.",
                },
                "top_k": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5,
                    "description": "How many procedures to return. Default 3.",
                },
            },
            "required": ["query"],
        },
    },
}

LOOKUP_COMPANY_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": LOOKUP_COMPANY,
        "description": "Find which of our companies the letter concerns, by name or number.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Company name or registration number found in the letter.",
                }
            },
            "required": ["query"],
        },
    },
}

PROPOSE_ACTION_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": PROPOSE_ACTION,
        "description": (
            "Queue the next move for a human to approve. This never executes anything: "
            "it writes a pending item and returns its id and the guard verdict. Call it "
            "once for every letter, including letters that need no action."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": [item.value for item in ActionKind],
                    "description": (
                        "pay or send move money or information and are checked against the "
                        "registry. file and contest are paperwork. escalate hands the letter "
                        "to a human. no_action files it."
                    ),
                },
                "payload": {
                    "type": "object",
                    "description": (
                        "Details of the move. For pay: payee, account, amount, currency, "
                        "reference, due_date. For send: to, subject, body_summary. For file "
                        "or contest: form, reference, deadline. For escalate: reason. "
                        "For no_action: leave empty."
                    ),
                    "additionalProperties": True,
                },
            },
            "required": ["kind", "payload"],
        },
    },
}

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    EXTRACT_FIELDS_SCHEMA,
    SEARCH_PLAYBOOK_SCHEMA,
    LOOKUP_COMPANY_SCHEMA,
    PROPOSE_ACTION_SCHEMA,
]


class SearchPlaybookArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=2)
    top_k: int = Field(default=3, ge=1, le=5)


class LookupCompanyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=2)


class ProposeActionArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ActionKind
    payload: dict[str, Any] = Field(default_factory=dict)


@dataclass
class ToolOutcome:
    """What one tool call produced, ready for the model and for the agent."""

    ok: bool
    payload: dict[str, Any]
    parsed: Any = None
    guard: GuardVerdict | None = None
    error: str | None = None
    retryable: bool = True

    def as_message(self) -> str:
        if self.ok:
            return json.dumps(self.payload, ensure_ascii=False, default=str)
        return json.dumps({"error": self.error}, ensure_ascii=False)


class ToolRunner:
    """Executes tool calls against the local index, registry and queue."""

    def __init__(
        self,
        *,
        retriever: Retriever,
        registry: Registry,
        queue: ActionQueue,
        run_id: str,
    ) -> None:
        self.retriever = retriever
        self.registry = registry
        self.queue = queue
        self.run_id = run_id

    def run(self, name: str, arguments_raw: str) -> ToolOutcome:
        if name not in TOOL_NAMES:
            return ToolOutcome(
                ok=False,
                payload={},
                error=f"unknown tool '{name}'. Available: {', '.join(TOOL_NAMES)}",
                retryable=False,
            )
        try:
            arguments = json.loads(arguments_raw) if arguments_raw.strip() else {}
        except json.JSONDecodeError as exc:
            return ToolOutcome(
                ok=False,
                payload={},
                error=f"arguments are not valid JSON: {exc.msg}",
            )
        if not isinstance(arguments, dict):
            return ToolOutcome(
                ok=False,
                payload={},
                error="arguments must be a JSON object",
            )
        handler = {
            EXTRACT_FIELDS: self._extract_fields,
            SEARCH_PLAYBOOK: self._search_playbook,
            LOOKUP_COMPANY: self._lookup_company,
            PROPOSE_ACTION: self._propose_action,
        }[name]
        return handler(arguments)

    def _extract_fields(self, arguments: dict[str, Any]) -> ToolOutcome:
        try:
            fields = LetterFields.model_validate(arguments)
        except ValidationError as exc:
            return ToolOutcome(
                ok=False,
                payload={},
                error=f"extract_fields arguments rejected: {_brief(exc)}",
            )
        return ToolOutcome(
            ok=True,
            payload=fields.model_dump(mode="json"),
            parsed=fields,
        )

    def _search_playbook(self, arguments: dict[str, Any]) -> ToolOutcome:
        try:
            args = SearchPlaybookArgs.model_validate(arguments)
        except ValidationError as exc:
            return ToolOutcome(
                ok=False,
                payload={},
                error=f"search_playbook arguments rejected: {_brief(exc)}",
            )
        hits = self.retriever.search(args.query, top_k=args.top_k)
        return ToolOutcome(
            ok=True,
            parsed=hits,
            payload={
                "query": args.query,
                "procedures": [
                    {
                        "playbook_id": hit.playbook_id,
                        "title": hit.title,
                        "owner": hit.owner,
                        "deadline_rule": hit.deadline_rule,
                        "excerpt": hit.text[:600],
                        "score": round(hit.score, 4),
                        "stage": hit.stage,
                    }
                    for hit in hits
                ],
            },
        )

    def _lookup_company(self, arguments: dict[str, Any]) -> ToolOutcome:
        try:
            args = LookupCompanyArgs.model_validate(arguments)
        except ValidationError as exc:
            return ToolOutcome(
                ok=False,
                payload={},
                error=f"lookup_company arguments rejected: {_brief(exc)}",
            )
        matches = self.registry.lookup(args.query)
        return ToolOutcome(
            ok=True,
            parsed=matches,
            payload={
                "query": args.query,
                "matches": [
                    {
                        "name": company.get("name"),
                        "number": company.get("number"),
                        "city": company.get("city"),
                        "contacts": company.get("contacts", []),
                    }
                    for company in matches
                ],
            },
        )

    def _propose_action(self, arguments: dict[str, Any]) -> ToolOutcome:
        try:
            args = ProposeActionArgs.model_validate(arguments)
        except ValidationError as exc:
            return ToolOutcome(
                ok=False,
                payload={},
                error=f"propose_action arguments rejected: {_brief(exc)}",
            )
        verdict = check_action(args.kind, args.payload, self.registry)
        if args.kind is ActionKind.NO_ACTION:
            return ToolOutcome(
                ok=True,
                parsed=None,
                guard=verdict,
                payload={
                    "queued": False,
                    "kind": args.kind.value,
                    "note": "no action to approve; the letter is filed",
                },
            )
        item = self.queue.add(
            run_id=self.run_id,
            kind=args.kind,
            payload=args.payload,
            blocked=not verdict.allowed,
            guard_reasons=verdict.reasons,
        )
        return ToolOutcome(
            ok=True,
            parsed=item,
            guard=verdict,
            payload={
                "queued": True,
                "pending_id": item.id,
                "kind": item.kind.value,
                "blocked": item.blocked,
                "guard": verdict.reasons,
                "note": (
                    "blocked by the deterministic payee guard; a human must resolve it"
                    if item.blocked
                    else "waiting for a human to approve"
                ),
            },
        )


def _brief(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors()[:4]:
        location = ".".join(str(item) for item in error["loc"]) or "(root)"
        parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts)


__all__ = [
    "EXTRACT_FIELDS",
    "LOOKUP_COMPANY",
    "PROPOSE_ACTION",
    "SEARCH_PLAYBOOK",
    "TOOL_DEFINITIONS",
    "TOOL_NAMES",
    "LookupCompanyArgs",
    "ProposeActionArgs",
    "SearchPlaybookArgs",
    "ToolOutcome",
    "ToolRunner",
]
