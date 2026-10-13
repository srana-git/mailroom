"""Registry lookup and the payee matching rules."""

from __future__ import annotations

import json
from pathlib import Path

from mailroom.registry import Registry, load_registry, normalize_text


def test_normalize_text_drops_accents_case_and_punctuation():
    assert normalize_text("Boulangerie Exemple Ltée,") == "boulangerie exemple ltee"
    assert normalize_text("  REVENU   QUÉBEC  ") == "revenu quebec"


def test_lookup_by_registration_number(repo_root: Path):
    registry = load_registry(repo_root / "data" / "registry.json")
    matches = registry.lookup("1199999001")
    assert matches
    assert matches[0]["name"] == "Exemple Logistique Inc."


def test_lookup_by_name_is_accent_insensitive(repo_root: Path):
    registry = load_registry(repo_root / "data" / "registry.json")
    matches = registry.lookup("boulangerie exemple ltee")
    assert matches
    assert matches[0]["number"] == "1199999002"


def test_lookup_of_an_unknown_name_returns_nothing(repo_root: Path):
    registry = load_registry(repo_root / "data" / "registry.json")
    assert registry.lookup("Entreprise Inexistante") == []


def test_lookup_of_an_empty_query_returns_nothing(repo_root: Path):
    registry = load_registry(repo_root / "data" / "registry.json")
    assert registry.lookup("   ") == []


def test_known_payee_by_name(repo_root: Path):
    registry = load_registry(repo_root / "data" / "registry.json")
    assert registry.is_known_payee("Revenu Québec", None) is True


def test_known_payee_by_account_ignores_separators(repo_root: Path):
    registry = load_registry(repo_root / "data" / "registry.json")
    assert registry.is_known_payee(None, "1199999001 01") is True


def test_known_payee_by_email(repo_root: Path):
    registry = load_registry(repo_root / "data" / "registry.json")
    assert registry.is_known_payee("Paiements@RevenuQuebec.example", None) is True


def test_unknown_account_is_not_a_match_even_with_a_real_name(repo_root: Path):
    """The attack this guard exists for: a real agency name with a new account."""
    registry = load_registry(repo_root / "data" / "registry.json")
    assert registry.is_known_payee("Revenu Québec", "8842119-447") is False


def test_a_verbose_payee_name_still_matches_on_the_account(repo_root: Path):
    registry = load_registry(repo_root / "data" / "registry.json")
    assert registry.is_known_payee(
        "Revenu Québec - Direction principale des taxes", "1199999001-01"
    ) is True


def test_unknown_name_with_no_account_is_not_a_match(repo_root: Path):
    registry = load_registry(repo_root / "data" / "registry.json")
    assert registry.is_known_payee("Comptoir Exemple SARL", None) is False


def test_no_identifiers_at_all_is_not_a_match(repo_root: Path):
    registry = load_registry(repo_root / "data" / "registry.json")
    assert registry.is_known_payee(None, None) is False


def test_registry_loads_from_a_minimal_file(tmp_path: Path):
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"companies": [{"name": "A Inc.", "number": "1199999009"}]}))
    registry = Registry.load(path)
    assert registry.lookup("1199999009")[0]["name"] == "A Inc."
    assert registry.payees == []
