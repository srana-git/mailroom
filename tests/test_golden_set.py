"""The golden set has to be right before any number it produces means anything."""

from __future__ import annotations

import collections
import json
from datetime import date
from pathlib import Path

import pytest

from mailroom.chunking import load_playbook_chunks
from mailroom.evaluation.harness import load_golden
from mailroom.models import ActionKind, LetterType, RequiredAction
from mailroom.registry import normalize_text

GOLDEN = Path(__file__).resolve().parent.parent / "evals" / "golden"
EXPECTED_FAMILIES = {
    "routine": 8,
    "payment": 8,
    "final": 6,
    "registry": 6,
    "info": 6,
    "payroll": 5,
    "phishing": 5,
    "injection": 6,
    "ocr": 5,
    "ambiguous": 5,
}


@pytest.fixture(scope="module")
def raw_cases() -> list[dict]:
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(GOLDEN.glob("*.json"))]


def test_the_set_holds_sixty_letters(raw_cases: list[dict]):
    assert len(raw_cases) == 60


def test_family_counts_match_the_spec(raw_cases: list[dict]):
    counts = collections.Counter(case["family"] for case in raw_cases)
    assert dict(counts) == EXPECTED_FAMILIES


def test_the_two_languages_are_balanced_within_ten_percent(raw_cases: list[dict]):
    counts = collections.Counter(case["language"] for case in raw_cases)
    assert counts["fr"] + counts["en"] == 60
    assert abs(counts["fr"] - counts["en"]) <= 6


def test_ids_are_unique_and_match_the_file_name():
    for path in sorted(GOLDEN.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["id"] == path.stem


def test_every_case_declares_a_gold_label_for_every_field(raw_cases: list[dict]):
    required = {
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
    for case in raw_cases:
        assert required <= set(case["gold"]), case["id"]


def test_gold_letter_type_and_action_are_known_values(raw_cases: list[dict]):
    for case in raw_cases:
        LetterType(case["gold"]["letter_type"])
        RequiredAction(case["gold"]["required_action"])
        ActionKind(case["gold_action_kind"])


def test_gold_urgency_is_within_range(raw_cases: list[dict]):
    for case in raw_cases:
        assert 1 <= case["gold"]["urgency"] <= 5, case["id"]


def test_gold_dates_are_iso_and_in_the_future_of_the_set(raw_cases: list[dict]):
    for case in raw_cases:
        value = case["gold"]["due_date"]
        if value is None:
            continue
        assert date.fromisoformat(value).year == 2026, case["id"]


def test_gold_amounts_are_two_decimal_numbers(raw_cases: list[dict]):
    for case in raw_cases:
        value = case["gold"]["amount_due"]
        if value is None:
            continue
        assert isinstance(value, (int, float)), case["id"]
        assert round(float(value), 2) == float(value), case["id"]
        assert case["gold"]["currency"] == "CAD", case["id"]


def test_the_declared_language_matches_the_gold_field(raw_cases: list[dict]):
    for case in raw_cases:
        assert case["language"] == case["gold"]["language"], case["id"]


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


def test_ambiguous_cases_escalate(raw_cases: list[dict]):
    for case in raw_cases:
        if case["family"] == "ambiguous":
            assert case["gold"]["required_action"] == "escalate", case["id"]
            assert case["gold_needs_human"] is True, case["id"]
            assert case["gold_action_kind"] == "escalate", case["id"]


def test_at_least_one_case_per_config_is_a_needs_human_positive(raw_cases: list[dict]):
    positives = [case for case in raw_cases if case["gold_needs_human"]]
    assert len(positives) >= 10


def test_no_real_organisation_is_named_as_a_company(raw_cases: list[dict]):
    """Companies must be the invented 'Exemple' family, never a real business.

    Numbered companies such as "9185-2204 Quebec Inc." are skipped: a number
    cannot name a real business.
    """
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


def test_the_loader_agrees_with_the_raw_files(raw_cases: list[dict]):
    cases = load_golden(GOLDEN)
    assert len(cases) == len(raw_cases)
    assert {case.id for case in cases} == {case["id"] for case in raw_cases}


def test_the_loader_can_limit():
    assert len(load_golden(GOLDEN, limit=7)) == 7


def _company_like(case: dict) -> list[str]:
    """Names in the letter that end in a corporate suffix.

    A leading Quebec company number (1234-5678) is kept in the match, so the
    test can tell a numbered company apart from a named one.
    """
    import re

    suffix = r"(?:Inc\.?|Lt[eé]e|SENC|Corp\.?|Limit[eé]e|SARL|S\.A\.R\.L\.?|Services Inc\.?)"
    number = r"(?:\d{4}-\d{4}\s+)?"
    pattern = rf"({number}[A-ZÉÈÀÂ][\w'’\-]*(?:\s+[A-ZÉÈÀÂ][\w'’\-]*)*\s+{suffix})"
    return [match.strip() for match in re.findall(pattern, case["letter_text"])]
