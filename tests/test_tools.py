"""Tool dispatch: argument validation, the queue write, and the guard verdict."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mailroom.models import ActionKind, LetterFields
from mailroom.queue import ActionQueue
from mailroom.registry import Registry, load_registry
from mailroom.retrieval import Retriever
from mailroom.tools import (
    EXTRACT_FIELDS,
    LOOKUP_COMPANY,
    PROPOSE_ACTION,
    SEARCH_PLAYBOOK,
    TOOL_DEFINITIONS,
    ToolOutcome,
    ToolRunner,
)

VALID_FIELDS = {
    "issuer": "Revenu Québec",
    "letter_type": "payment_demand",
    "language": "en",
    "reference_number": "RQ-1",
    "amount_due": 10.0,
    "currency": "CAD",
    "due_date": "2026-11-14",
    "required_action": "pay",
    "urgency": 4,
    "is_suspicious": False,
    "suspicion_reason": None,
}


@pytest.fixture
def registry(repo_root: Path) -> Registry:
    return load_registry(repo_root / "data" / "registry.json")


@pytest.fixture
def runner(
    retriever: Retriever, registry: Registry, action_queue: ActionQueue
) -> ToolRunner:
    return ToolRunner(
        retriever=retriever, registry=registry, queue=action_queue, run_id="run1"
    )


def test_every_tool_definition_has_a_name_and_parameters():
    names = set()
    for definition in TOOL_DEFINITIONS:
        function = definition["function"]
        names.add(function["name"])
        assert function["description"]
        assert function["parameters"]["type"] == "object"
    assert names == {EXTRACT_FIELDS, SEARCH_PLAYBOOK, LOOKUP_COMPANY, PROPOSE_ACTION}


def test_an_unknown_tool_is_not_retryable(runner: ToolRunner):
    outcome = runner.run("delete_everything", "{}")
    assert outcome.ok is False
    assert outcome.retryable is False
    assert "unknown tool" in outcome.error


def test_arguments_that_are_not_json_are_retryable(runner: ToolRunner):
    outcome = runner.run(EXTRACT_FIELDS, "{oops")
    assert outcome.ok is False
    assert outcome.retryable is True
    assert "not valid JSON" in outcome.error


def test_arguments_that_are_not_an_object_are_retryable(runner: ToolRunner):
    outcome = runner.run(EXTRACT_FIELDS, "[1, 2]")
    assert outcome.ok is False
    assert "must be a JSON object" in outcome.error


def test_extract_fields_returns_a_validated_model(runner: ToolRunner):
    outcome = runner.run(EXTRACT_FIELDS, json.dumps(VALID_FIELDS))
    assert outcome.ok is True
    assert isinstance(outcome.parsed, LetterFields)
    assert outcome.payload["currency"] == "CAD"


def test_extract_fields_names_the_bad_field_when_it_rejects_arguments(runner: ToolRunner):
    broken = dict(VALID_FIELDS, urgency=9)
    outcome = runner.run(EXTRACT_FIELDS, json.dumps(broken))
    assert outcome.ok is False
    assert "urgency" in outcome.error


def test_search_playbook_requires_a_query(runner: ToolRunner):
    outcome = runner.run(SEARCH_PLAYBOOK, json.dumps({"query": ""}))
    assert outcome.ok is False
    assert "query" in outcome.error


def test_search_playbook_rejects_an_out_of_range_top_k(runner: ToolRunner):
    outcome = runner.run(SEARCH_PLAYBOOK, json.dumps({"query": "tax notice", "top_k": 50}))
    assert outcome.ok is False


def test_search_playbook_returns_procedures_with_ids(runner: ToolRunner):
    outcome = runner.run(SEARCH_PLAYBOOK, json.dumps({"query": "final notice tax payment"}))
    assert outcome.ok is True
    procedures = outcome.payload["procedures"]
    assert 1 <= len(procedures) <= 3
    for procedure in procedures:
        assert procedure["playbook_id"].startswith("PB-")


def test_search_playbook_defaults_top_k_to_three(runner: ToolRunner):
    outcome = runner.run(SEARCH_PLAYBOOK, json.dumps({"query": "registry declaration"}))
    assert len(outcome.payload["procedures"]) <= 3


def test_lookup_company_finds_a_registered_number(runner: ToolRunner):
    outcome = runner.run(LOOKUP_COMPANY, json.dumps({"query": "1199999001"}))
    assert outcome.ok is True
    assert outcome.payload["matches"][0]["name"] == "Exemple Logistique Inc."


def test_lookup_company_with_an_unknown_name_returns_an_empty_list(runner: ToolRunner):
    outcome = runner.run(LOOKUP_COMPANY, json.dumps({"query": "Nothing At All"}))
    assert outcome.payload["matches"] == []


def test_propose_pay_to_a_registry_account_queues_an_unblocked_item(
    runner: ToolRunner, action_queue: ActionQueue
):
    outcome = runner.run(
        PROPOSE_ACTION,
        json.dumps(
            {
                "kind": "pay",
                "payload": {"payee": "Revenu Québec", "account": "1199999001-01"},
            }
        ),
    )
    assert outcome.ok is True
    assert outcome.payload["queued"] is True
    assert outcome.payload["blocked"] is False
    assert action_queue.pending()[0].id == outcome.payload["pending_id"]


def test_propose_pay_to_an_unknown_account_queues_a_blocked_item(
    runner: ToolRunner, action_queue: ActionQueue
):
    outcome = runner.run(
        PROPOSE_ACTION,
        json.dumps(
            {
                "kind": "pay",
                "payload": {"payee": "Comptoir Exemple SARL", "account": "8842119-447"},
            }
        ),
    )
    assert outcome.payload["blocked"] is True
    assert outcome.guard.allowed is False
    assert action_queue.pending()[0].blocked is True


def test_propose_pay_still_queues_so_a_human_can_see_it(
    runner: ToolRunner, action_queue: ActionQueue
):
    runner.run(
        PROPOSE_ACTION, json.dumps({"kind": "pay", "payload": {"account": "8842119-447"}})
    )
    assert len(action_queue.all_items()) == 1


def test_propose_no_action_queues_nothing(runner: ToolRunner, action_queue: ActionQueue):
    outcome = runner.run(PROPOSE_ACTION, json.dumps({"kind": "no_action", "payload": {}}))
    assert outcome.ok is True
    assert outcome.payload["queued"] is False
    assert action_queue.all_items() == []


def test_propose_action_rejects_an_unknown_kind(runner: ToolRunner):
    outcome = runner.run(PROPOSE_ACTION, json.dumps({"kind": "wire", "payload": {}}))
    assert outcome.ok is False
    assert outcome.retryable is True


def test_the_queue_item_records_the_run_id(runner: ToolRunner, action_queue: ActionQueue):
    runner.run(PROPOSE_ACTION, json.dumps({"kind": "file", "payload": {"form": "annual"}}))
    assert action_queue.pending()[0].run_id == "run1"


def test_a_failed_outcome_serialises_to_an_error_message():
    message = ToolOutcome(ok=False, payload={}, error="bad").as_message()
    assert json.loads(message) == {"error": "bad"}


def test_the_guard_verdict_is_attached_to_a_file_action(runner: ToolRunner):
    outcome = runner.run(PROPOSE_ACTION, json.dumps({"kind": "contest", "payload": {}}))
    assert outcome.guard is not None
    assert outcome.guard.allowed is True


def test_propose_action_defaults_an_absent_payload(runner: ToolRunner):
    outcome = runner.run(PROPOSE_ACTION, json.dumps({"kind": "escalate"}))
    assert outcome.ok is True
    assert outcome.payload["queued"] is True
    assert outcome.parsed.kind is ActionKind.ESCALATE
