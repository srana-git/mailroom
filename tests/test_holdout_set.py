"""The held-out set: shape only.

This set is never used for tuning, so nothing here checks what the pipeline does
with a letter. These tests only prove that the set is well formed: the right
families, the right language balance, labels that exist in the corpus, and no
real organisation or address anywhere in it.
"""

from __future__ import annotations

import collections
import json
import re
from datetime import date
from pathlib import Path

import pytest

from mailroom.chunking import load_playbook_chunks
from mailroom.evaluation.harness import load_golden
from mailroom.models import ActionKind, LetterType, RequiredAction
from mailroom.registry import normalize_text

HOLDOUT = Path(__file__).resolve().parent.parent / "evals" / "holdout"
EXPECTED_FAMILIES = {
    "routine": 2,
    "payment": 2,
    "final": 2,
    "registry": 2,
    "info": 2,
    "payroll": 2,
    "phishing": 2,
    "injection": 2,
    "ocr": 2,
    "ambiguous": 2,
}
GOLD_FIELDS = {
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
}

# Unambiguous function words, used only to tell a French letterhead from a
# French body. No pipeline code is involved.
FRENCH_WORDS = (
    "des",
    "du",
    "aux",
    "les",
    "votre",
    "vos",
    "objet",
    "aucune",
    "de la",
    "au plus",
    "a la",
)


@pytest.fixture(scope="module")
def raw_cases() -> list[dict]:
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(HOLDOUT.glob("*.json"))]


def test_the_holdout_holds_twenty_letters(raw_cases: list[dict]):
    assert len(raw_cases) == 20


def test_the_family_mix_is_the_development_mix_in_miniature(raw_cases: list[dict]):
    counts = collections.Counter(case["family"] for case in raw_cases)
    assert dict(counts) == EXPECTED_FAMILIES


def test_the_two_languages_are_declared_in_equal_number(raw_cases: list[dict]):
    counts = collections.Counter(case["language"] for case in raw_cases)
    assert counts == {"fr": 10, "en": 10}


def test_ids_are_unique_and_match_the_file_name():
    for path in sorted(HOLDOUT.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["id"] == path.stem


def test_every_case_declares_a_gold_label_for_every_field(raw_cases: list[dict]):
    for case in raw_cases:
        assert set(case["gold"]) >= GOLD_FIELDS, case["id"]


def test_gold_values_are_known_enum_members(raw_cases: list[dict]):
    for case in raw_cases:
        LetterType(case["gold"]["letter_type"])
        RequiredAction(case["gold"]["required_action"])
        ActionKind(case["gold_action_kind"])


def test_the_declared_language_matches_the_gold_field(raw_cases: list[dict]):
    for case in raw_cases:
        assert case["language"] == case["gold"]["language"], case["id"]


def test_gold_dates_are_iso_and_in_2026(raw_cases: list[dict]):
    for case in raw_cases:
        value = case["gold"]["due_date"]
        if value is None:
            continue
        assert date.fromisoformat(value).year == 2026, case["id"]


def test_a_stated_amount_carries_a_currency(raw_cases: list[dict]):
    for case in raw_cases:
        if case["gold"]["amount_due"] is not None:
            assert case["gold"]["currency"] == "CAD", case["id"]


def test_gold_playbooks_exist_in_the_corpus(raw_cases: list[dict], repo_root: Path):
    known = {chunk.playbook_id for chunk in load_playbook_chunks(repo_root / "data" / "playbook")}
    for case in raw_cases:
        assert case["gold_playbooks"], case["id"]
        for playbook_id in case["gold_playbooks"]:
            assert playbook_id in known, f"{case['id']} -> {playbook_id}"


def test_suspicion_labels_are_internally_consistent(raw_cases: list[dict]):
    for case in raw_cases:
        gold = case["gold"]
        if gold["is_suspicious"]:
            assert gold["suspicion_reason"], case["id"]
        else:
            assert gold["suspicion_reason"] is None, case["id"]


def test_phishing_and_injection_cases_are_all_suspicious(raw_cases: list[dict]):
    for case in raw_cases:
        if case["family"] in ("phishing", "injection"):
            assert case["gold"]["is_suspicious"] is True, case["id"]


def test_injection_cases_carry_the_target_the_letter_asked_for(raw_cases: list[dict]):
    for case in raw_cases:
        if case["family"] == "injection":
            target = case.get("gold_injection_target")
            assert target, case["id"]
            assert any(value for value in target.values()), case["id"]


def test_injection_cases_expect_a_human_not_a_queued_action(raw_cases: list[dict]):
    """A letter that carries agent-directed instructions is reviewed by a human."""
    for case in raw_cases:
        if case["family"] == "injection":
            assert case["gold_action_kind"] == "escalate", case["id"]
            assert case["gold_needs_human"] is True, case["id"]


def test_ambiguous_cases_escalate(raw_cases: list[dict]):
    for case in raw_cases:
        if case["family"] == "ambiguous":
            assert case["gold"]["required_action"] == "escalate", case["id"]
            assert case["gold_needs_human"] is True, case["id"]


def test_at_least_four_letters_mix_the_letterhead_language_and_the_body(raw_cases: list[dict]):
    """A French letterhead over an English body, or the reverse."""
    mixed = [case["id"] for case in raw_cases if _mixes_letterhead_and_body(case["letter_text"])]
    assert len(mixed) >= 4, mixed


def test_no_real_organisation_is_named_as_a_company(raw_cases: list[dict]):
    for case in raw_cases:
        for match in _company_like(case):
            if match[0].isdigit():
                continue
            assert "exemple" in normalize_text(match), f"{case['id']}: {match}"


def test_no_real_email_domain_appears(raw_cases: list[dict]):
    for case in raw_cases:
        for token in case["letter_text"].split():
            if "@" in token:
                assert ".example" in token, f"{case['id']}: {token}"


def test_the_loader_reads_the_holdout_set(raw_cases: list[dict]):
    cases = load_golden(HOLDOUT)
    assert len(cases) == len(raw_cases)
    assert {case.id for case in cases} == {case["id"] for case in raw_cases}


def _mixes_letterhead_and_body(letter_text: str) -> bool:
    blocks = [block for block in letter_text.split("\n\n") if block.strip()]
    if len(blocks) < 2:
        return False
    head = normalize_text(blocks[0])
    body = normalize_text("\n\n".join(blocks[1:]))
    head_is_french = any(word in head for word in FRENCH_WORDS)
    body_is_french = sum(1 for word in FRENCH_WORDS if word in body) >= 2
    return head_is_french != body_is_french


def _company_like(case: dict) -> list[str]:
    suffix = r"(?:Inc\.?|Lt[eé]e|SENC|Corp\.?|Limit[eé]e|SARL|S\.A\.R\.L\.?|Services Inc\.?)"
    number = r"(?:\d{4}-\d{4}\s+)?"
    pattern = rf"({number}[A-ZÉÈÀÂ][\w'’\-]*(?:\s+[A-ZÉÈÀÂ][\w'’\-]*)*\s+{suffix})"
    return [match.strip() for match in re.findall(pattern, case["letter_text"])]
