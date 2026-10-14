"""The triage loop: happy path, re-ask, fail closed, injection, audit."""

from __future__ import annotations

import json
from pathlib import Path

from mailroom.agent import Triager, _assistant_message
from mailroom.audit import read_run
from mailroom.config import Settings
from mailroom.fake import FakeBackend
from mailroom.indexer import PlaybookIndex
from mailroom.llm import BackendError, LLMResponse, ToolCallRequest
from mailroom.models import TriageStatus
from mailroom.queue import ActionQueue
from mailroom.registry import Registry
from mailroom.retrieval import Retriever

VALID_FIELDS = {
    "issuer": "Revenu Québec",
    "letter_type": "payment_demand",
    "language": "en",
    "reference_number": "RQ-2026-4471199",
    "amount_due": 3412.55,
    "currency": "CAD",
    "due_date": "2026-11-14",
    "required_action": "pay",
    "urgency": 4,
    "is_suspicious": False,
    "suspicion_reason": None,
}

PAYMENT_LETTER = (Path(__file__).resolve().parent.parent / "evals/golden/payment-01.json")
INJECTION_LETTER = (Path(__file__).resolve().parent.parent / "evals/golden/injection-01.json")


def _letter(path: Path) -> str:
    return json.loads(path.read_text(encoding="utf-8"))["letter_text"]


def _scripted(
    settings: Settings,
    registry: Registry,
    script: list,
    *,
    index: PlaybookIndex,
) -> tuple[Triager, ActionQueue, FakeBackend]:
    backend = FakeBackend(script=script)
    queue = ActionQueue(settings.queue_path)
    retriever = Retriever(settings, backend, index=index)
    triager = Triager(
        backend=backend,
        retriever=retriever,
        registry=registry,
        queue=queue,
        settings=settings,
    )
    return triager, queue, backend


def test_a_payment_letter_queues_an_action_and_keeps_the_facts(triager: Triager):
    result = triager.run(_letter(PAYMENT_LETTER), source="payment-01")
    assert result.status is TriageStatus.ACTION_PENDING
    assert result.fields is not None
    assert result.fields.amount_due == 3412.55
    assert result.fields.reference_number == "RQ-2026-4471199"
    assert [action.kind.value for action in result.actions] == ["pay"]
    assert result.blocked_actions == []


def test_the_run_writes_a_start_and_an_end_to_the_trail(triager: Triager, settings: Settings):
    result = triager.run("A short letter that asks for nothing.", source="tiny")
    events = read_run(result.run_id, settings.runs_dir)
    kinds = [event["kind"] for event in events]
    assert kinds[0] == "run_start"
    assert kinds[-1] == "run_end"
    assert events[-1]["status"] == result.status.value


def test_every_tool_call_is_in_the_trail_with_its_guard(triager: Triager, settings: Settings):
    result = triager.run(_letter(PAYMENT_LETTER), source="payment-01")
    events = read_run(result.run_id, settings.runs_dir)
    tools = [event for event in events if event["kind"] == "tool_call"]
    names = [event["tool"] for event in tools]
    assert "extract_fields" in names
    assert "propose_action" in names
    propose = next(event for event in tools if event["tool"] == "propose_action")
    assert propose["guard"]["allowed"] is True


def test_an_injection_letter_is_routed_to_a_human_with_a_blocked_action(triager: Triager):
    result = triager.run(_letter(INJECTION_LETTER), source="injection-01")
    assert result.status is TriageStatus.NEEDS_HUMAN
    assert result.injection_flags
    assert "instructions aimed at the agent" in result.reason


def test_the_injected_account_never_reaches_an_unblocked_action(triager: Triager):
    result = triager.run(_letter(INJECTION_LETTER), source="injection-01")
    unblocked = [action for action in result.actions if not action.blocked]
    for action in unblocked:
        assert "8842119-447" not in json.dumps(action.payload)


def test_the_injected_payment_is_blocked_by_the_guard(triager: Triager):
    result = triager.run(_letter(INJECTION_LETTER), source="injection-01")
    assert len(result.blocked_actions) == 1
    assert "not a registry payee" in result.reason


def test_an_information_only_letter_is_filed(triager: Triager):
    letter = (
        "Exemple Télécom\n\nExemple Conseil Inc.\n\n"
        "This notice is for information only. No action is required."
    )
    result = triager.run(letter, source="info")
    assert result.status is TriageStatus.FILED


def test_an_ambiguous_letter_escalates(
    settings: Settings, registry: Registry, index: PlaybookIndex
):
    script = [
        {"tool": "extract_fields", "args": dict(VALID_FIELDS, required_action="escalate")},
        {"tool": "propose_action", "args": {"kind": "escalate", "payload": {"reason": "unclear"}}},
    ]
    triager, _, _ = _scripted(settings, registry, script, index=index)
    result = triager.run("An unclear letter.", source="ambiguous")
    assert result.status is TriageStatus.NEEDS_HUMAN
    assert "does not settle" in result.reason


def test_a_rejected_argument_is_asked_again_once(
    settings: Settings, registry: Registry, index: PlaybookIndex
):
    script = [
        {"tool": "extract_fields", "args": {"issuer": "X"}},
        {"tool": "extract_fields", "args": VALID_FIELDS},
        {"tool": "propose_action", "args": {"kind": "file", "payload": {"form": "a"}}},
    ]
    triager, _, _ = _scripted(settings, registry, script, index=index)
    result = triager.run("A letter.", source="reask")
    assert result.status is TriageStatus.ACTION_PENDING
    assert result.fields is not None


def test_a_second_rejection_fails_closed(
    settings: Settings, registry: Registry, index: PlaybookIndex
):
    broken = {"tool": "extract_fields", "args": {"issuer": "X"}}
    triager, _, _ = _scripted(settings, registry, [broken, broken], index=index)
    result = triager.run("A letter.", source="fail-closed")
    assert result.status is TriageStatus.NEEDS_HUMAN
    assert "rejected twice" in result.reason


def test_an_unknown_tool_fails_closed_immediately(
    settings: Settings, registry: Registry, index: PlaybookIndex
):
    script = [{"tool": "read_the_letter", "args": {}}]
    triager, _, _ = _scripted(settings, registry, script, index=index)
    result = triager.run("A letter.", source="unknown-tool")
    assert result.status is TriageStatus.NEEDS_HUMAN
    assert "could not be called" in result.reason


def test_a_model_failure_fails_closed(settings: Settings, registry: Registry, index: PlaybookIndex):
    script = [BackendError("chat failed after 4 attempt(s) on m: 500", attempts=4)]
    triager, _, _ = _scripted(settings, registry, script, index=index)
    result = triager.run("A letter.", source="backend-error")
    assert result.status is TriageStatus.NEEDS_HUMAN
    assert "model call failed" in result.reason


def test_the_step_budget_is_enforced(settings: Settings, registry: Registry, index: PlaybookIndex):
    tight = Settings.build(
        root=settings.root,
        backend=settings.backend,
        state_dir=settings.state_dir,
        index_dir=settings.index_dir,
        queue_path=settings.queue_path,
        runs_dir=settings.runs_dir,
        max_steps=3,
    )
    script = [{"tool": "search_playbook", "args": {"query": "anything"}}] * 4
    triager, _, _ = _scripted(tight, registry, script, index=index)
    result = triager.run("A letter.", source="budget")
    assert result.steps == 3
    assert result.status is TriageStatus.NEEDS_HUMAN


def test_the_plan_is_recorded_and_defaults_when_unusable(
    settings: Settings, registry: Registry, index: PlaybookIndex
):
    script = [
        {"tool": "extract_fields", "args": VALID_FIELDS},
        {"tool": "propose_action", "args": {"kind": "file", "payload": {}}},
    ]
    triager, _, _ = _scripted(settings, registry, script, index=index)
    result = triager.run("A letter.", source="plan")
    assert len(result.plan) >= 3


def test_a_letter_that_is_never_read_needs_a_human(
    settings: Settings, registry: Registry, index: PlaybookIndex
):
    triager, _, _ = _scripted(settings, registry, ["I would rather not."], index=index)
    result = triager.run("A letter.", source="no-fields")
    assert result.status is TriageStatus.NEEDS_HUMAN
    assert "could not be read" in result.reason


def test_token_usage_is_totalled(triager: Triager):
    result = triager.run(_letter(PAYMENT_LETTER), source="payment-01")
    assert result.input_tokens > 0
    assert result.output_tokens > 0
    assert result.latency_ms >= 0


def test_the_company_lookup_result_is_kept(triager: Triager):
    result = triager.run(_letter(PAYMENT_LETTER), source="payment-01")
    assert isinstance(result.company_matches, list)


def test_retrieval_hits_carry_a_playbook_id(triager: Triager):
    result = triager.run(_letter(PAYMENT_LETTER), source="payment-01")
    assert result.retrieval
    assert all(hit.playbook_id.startswith("PB-") for hit in result.retrieval)


def test_a_suspicious_reading_needs_a_human(
    settings: Settings, registry: Registry, index: PlaybookIndex
):
    suspicious = dict(
        VALID_FIELDS,
        is_suspicious=True,
        suspicion_reason="spoofed sender domain",
    )
    script = [
        {"tool": "extract_fields", "args": suspicious},
        {"tool": "propose_action", "args": {"kind": "escalate", "payload": {}}},
    ]
    triager, _, _ = _scripted(settings, registry, script, index=index)
    result = triager.run("A letter.", source="suspicious")
    assert result.status is TriageStatus.NEEDS_HUMAN
    assert "suspected fraud" in result.reason


def test_an_action_that_was_expected_but_never_proposed_needs_a_human(
    settings: Settings, registry: Registry, index: PlaybookIndex
):
    script = [{"tool": "extract_fields", "args": VALID_FIELDS}]
    triager, _, _ = _scripted(settings, registry, script, index=index)
    result = triager.run("A letter.", source="no-action")
    assert result.status is TriageStatus.NEEDS_HUMAN
    assert "no action was proposed" in result.reason


def test_a_no_action_reading_with_nothing_queued_is_filed(
    settings: Settings, registry: Registry, index: PlaybookIndex
):
    script = [
        {"tool": "extract_fields", "args": dict(VALID_FIELDS, required_action="no_action")},
        {"tool": "propose_action", "args": {"kind": "no_action", "payload": {}}},
    ]
    triager, _, _ = _scripted(settings, registry, script, index=index)
    result = triager.run("A letter.", source="filed")
    assert result.status is TriageStatus.FILED
    assert result.actions == []


class _AuditAwareFake(FakeBackend):
    """A fake backend that offers `with_audit`, so the rebinding is observable."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.audit = None

    def with_audit(self, audit):
        clone = _AuditAwareFake()
        clone.audit = audit
        return clone


def test_the_retriever_backend_is_bound_to_the_runs_audit(
    settings: Settings,
    registry: Registry,
    index: PlaybookIndex,
    action_queue: ActionQueue,
):
    """Retrieval runs on the same audit sink as the chat calls, so it is logged."""
    backend = _AuditAwareFake()
    retriever = Retriever(settings, backend, index=index)
    triager = Triager(
        backend=backend,
        retriever=retriever,
        registry=registry,
        queue=action_queue,
        settings=settings,
    )
    result = triager.run("A short letter that asks for nothing.", source="bind")
    bound = triager.retriever.backend
    assert bound is not backend
    assert bound.audit is not None
    assert bound.audit.run_id == result.run_id


# -- the deterministic corrections -----------------------------------------

ENGLISH_BODY_UNDER_FRENCH_LETTERHEAD = (
    "Revenu Québec\nDirection principale des taxes\n\n"
    "We received your file and the amount is now settled. The balance is zero and no "
    "action is needed from you. Please keep this letter for your records.\n\n"
    "Direction principale des taxes"
)
GIFT_CARD_LETTER = (
    "Exemple Facturation\nService à la clientèle\n\n"
    "Votre compte présente un solde. Payez par cartes cadeaux aujourd'hui.\n\n"
    "Service à la clientèle"
)


def test_the_body_language_overrides_a_french_reading_of_an_english_body(
    settings: Settings, registry: Registry, index: PlaybookIndex
):
    script = [
        {"tool": "extract_fields", "args": dict(VALID_FIELDS, language="fr")},
        {"tool": "propose_action", "args": {"kind": "file", "payload": {}}},
    ]
    triager, _, _ = _scripted(settings, registry, script, index=index)
    result = triager.run(ENGLISH_BODY_UNDER_FRENCH_LETTERHEAD, source="language")
    assert result.fields is not None
    assert result.fields.language.value == "en"
    notes = [
        event
        for event in read_run(result.run_id, settings.runs_dir)
        if event["kind"] == "note" and event["message"].startswith("the body decides")
    ]
    assert notes
    assert notes[0]["model_language"] == "fr"
    assert notes[0]["detected_language"] == "en"


def test_a_fraud_signal_marks_the_letter_suspicious_over_the_model(
    settings: Settings, registry: Registry, index: PlaybookIndex
):
    script = [
        {"tool": "extract_fields", "args": dict(VALID_FIELDS, is_suspicious=False)},
        {"tool": "propose_action", "args": {"kind": "escalate", "payload": {}}},
    ]
    triager, _, _ = _scripted(settings, registry, script, index=index)
    result = triager.run(GIFT_CARD_LETTER, source="phishing-or")
    assert result.fields is not None
    assert result.fields.is_suspicious is True
    assert result.status is TriageStatus.NEEDS_HUMAN
    assert "suspected fraud" in result.reason


def test_the_scans_force_the_security_procedure_into_the_retrieval(
    triager: Triager, settings: Settings
):
    result = triager.run(_letter(INJECTION_LETTER), source="injection-01")
    assert result.retrieval
    assert result.retrieval[0].playbook_id == "PB-SEC-002"
    assert result.retrieval[0].stage == "forced"
    notes = [
        event
        for event in read_run(result.run_id, settings.runs_dir)
        if event["kind"] == "note"
        and event["message"].startswith("the scans put the security procedures")
    ]
    assert notes
    assert "PB-SEC-002" in notes[0]["procedures"]


# -- the assistant echo ----------------------------------------------------


def test_an_assistant_turn_with_tool_calls_carries_the_calls_and_the_plan():
    response = LLMResponse(
        text=None,
        tool_calls=[
            ToolCallRequest(
                id="call-1", name="extract_fields", arguments_raw='{"issuer": "Revenu"}'
            )
        ],
        tool_plan="read the fields, then queue the payment",
    )
    message = _assistant_message(response)
    assert message["role"] == "assistant"
    assert message["tool_calls"] == [
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "extract_fields", "arguments": '{"issuer": "Revenu"}'},
        }
    ]
    assert message["tool_plan"] == "read the fields, then queue the payment"
    assert "content" not in message


def test_an_assistant_turn_with_tool_calls_omits_a_missing_plan():
    response = LLMResponse(
        text=None, tool_calls=[ToolCallRequest(id="call-2", name="file", arguments_raw="{}")]
    )
    message = _assistant_message(response)
    assert "tool_calls" in message
    assert "tool_plan" not in message


def test_an_assistant_turn_without_tool_calls_carries_only_content():
    message = _assistant_message(LLMResponse(text="Filed as information only."))
    assert message == {"role": "assistant", "content": "Filed as information only."}
    assert "tool_calls" not in message
    assert "tool_plan" not in message


def test_an_assistant_turn_with_no_text_and_no_calls_carries_empty_content():
    assert _assistant_message(LLMResponse(text=None))["content"] == ""
