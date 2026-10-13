"""Field model: validation, defaults and the taxonomy maps."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from mailroom.models import (
    REQUIRED_ACTION_TO_KIND,
    WRITE_KINDS,
    ActionKind,
    LetterFields,
    LetterType,
    RequiredAction,
    TriageStatus,
)


def _fields(**overrides: object) -> dict:
    base = {
        "issuer": "Revenu Québec",
        "letter_type": "payment_demand",
        "language": "en",
        "required_action": "pay",
        "urgency": 4,
    }
    base.update(overrides)
    return base


def test_minimal_fields_validate():
    fields = LetterFields.model_validate(_fields())
    assert fields.amount_due is None
    assert fields.currency is None
    assert fields.due_date is None
    assert fields.is_suspicious is False


def test_currency_is_uppercased():
    fields = LetterFields.model_validate(_fields(currency=" cad "))
    assert fields.currency == "CAD"


def test_blank_currency_becomes_none():
    assert LetterFields.model_validate(_fields(currency="   ")).currency is None


def test_unknown_field_is_rejected():
    with pytest.raises(ValidationError):
        LetterFields.model_validate(_fields(confidence=0.9))


def test_unknown_letter_type_is_rejected():
    with pytest.raises(ValidationError):
        LetterFields.model_validate(_fields(letter_type="invoice_of_doom"))


def test_unknown_required_action_is_rejected():
    with pytest.raises(ValidationError):
        LetterFields.model_validate(_fields(required_action="wire"))


@pytest.mark.parametrize("urgency", [0, 6, -1, 99])
def test_urgency_outside_one_to_five_is_rejected(urgency: int):
    with pytest.raises(ValidationError):
        LetterFields.model_validate(_fields(urgency=urgency))


def test_due_date_accepts_iso_string():
    fields = LetterFields.model_validate(_fields(due_date="2026-11-14"))
    assert fields.due_date == date(2026, 11, 14)


def test_required_action_maps_to_a_kind():
    assert REQUIRED_ACTION_TO_KIND[RequiredAction.PAY] is ActionKind.PAY
    assert REQUIRED_ACTION_TO_KIND[RequiredAction.FILE_DECLARATION] is ActionKind.FILE
    assert REQUIRED_ACTION_TO_KIND[RequiredAction.NO_ACTION] is ActionKind.NO_ACTION


def test_write_kinds_exclude_no_action_and_escalate():
    assert ActionKind.PAY in WRITE_KINDS
    assert ActionKind.SEND in WRITE_KINDS
    assert ActionKind.FILE in WRITE_KINDS
    assert ActionKind.CONTEST in WRITE_KINDS
    assert ActionKind.NO_ACTION not in WRITE_KINDS
    assert ActionKind.ESCALATE not in WRITE_KINDS


def test_every_required_action_has_a_kind():
    assert set(REQUIRED_ACTION_TO_KIND) == set(RequiredAction)


def test_every_letter_type_is_a_known_type():
    assert {item.value for item in LetterType} >= {"phishing", "final_notice"}


def test_status_vocabulary():
    assert {item.value for item in TriageStatus} == {
        "filed",
        "action_pending",
        "needs_human",
        "error",
    }
