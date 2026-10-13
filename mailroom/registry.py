"""Fictional company registry.

Two jobs:

1. `lookup_company` answers "which of our companies is this letter about?".
2. The payee guard reads `known_payees` to decide whether a proposed payment or
   send has an approved destination.

Every entry is invented. Company numbers start 1199999... so they can never be
mistaken for a real registration.
"""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path
from typing import Any

_PUNCTUATION = ".,;:()[]{}'\"’“”«»/\\-_&"


def normalize_text(text: str) -> str:
    """Casefold, drop accents, drop punctuation, collapse whitespace.

    Used for every name comparison in this module so that "Boulangerie Exemple
    Ltée" and "BOULANGERIE EXEMPLE LTEE," compare equal.
    """
    decomposed = unicodedata.normalize("NFKD", str(text))
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    table = {ord(ch): " " for ch in _PUNCTUATION}
    cleaned = stripped.casefold().translate(table)
    return " ".join(cleaned.split())


def _digits(text: str) -> str:
    return "".join(ch for ch in str(text) if ch.isdigit())


class Registry:
    """Loaded registry, ready for lookups."""

    def __init__(self, companies: list[dict[str, Any]], payees: list[dict[str, Any]]) -> None:
        self._companies = list(companies)
        self._payees = list(payees)
        self._index: list[tuple[dict[str, Any], str, str]] = [
            (company, normalize_text(company.get("name", "")), _digits(company.get("number", "")))
            for company in self._companies
        ]
        self._payee_index: list[tuple[dict[str, Any], set[str], str, str]] = [
            (
                payee,
                {
                    normalize_text(payee.get(field, ""))
                    for field in ("name", "email")
                    if payee.get(field)
                },
                normalize_text(payee.get("account", "")),
                _digits(payee.get("account", "")),
            )
            for payee in self._payees
        ]

    @classmethod
    def load(cls, path: Path) -> Registry:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(data.get("companies", []), data.get("known_payees", []))

    @property
    def companies(self) -> list[dict[str, Any]]:
        return list(self._companies)

    @property
    def payees(self) -> list[dict[str, Any]]:
        return list(self._payees)

    def lookup(self, query: str, limit: int = 3) -> list[dict[str, Any]]:
        """Find companies matching a name or registration number.

        Ranked: exact number match, then exact name match, then substring match.
        """
        raw = str(query or "").strip()
        if not raw:
            return []
        wanted_name = normalize_text(raw)
        wanted_digits = _digits(raw)
        scored: list[tuple[int, dict[str, Any]]] = []
        for company, name, number in self._index:
            if wanted_digits and number and wanted_digits == number:
                scored.append((0, company))
            elif wanted_name and name and wanted_name == name:
                scored.append((1, company))
            elif wanted_name and name and (wanted_name in name or name in wanted_name):
                scored.append((2, company))
        scored.sort(key=lambda item: item[0])
        return [company for _, company in scored[:limit]]

    def match_payee(self, name: str | None, account: str | None) -> dict[str, Any] | None:
        """Return the registry payee this destination corresponds to, else None.

        The account decides when one is given. A stated account that is not in the
        registry is a mismatch even when the payee name is one we know: reusing a
        real agency name with a new account number is exactly the fraud this guard
        exists to stop. The account is compared on digits alone, so separators and
        spacing do not matter.

        When no account is given, the name decides. The name is matched against
        both the payee name and the payee email, because a send names a recipient
        rather than an account.
        """
        account_key = normalize_text(account or "")
        if account_key:
            account_digits = _digits(account)
            for payee, _names, payee_account, payee_digits in self._payee_index:
                if account_digits and payee_digits and account_digits == payee_digits:
                    return payee
                if payee_account and account_key == payee_account:
                    return payee
            return None
        name_key = normalize_text(name or "")
        if not name_key:
            return None
        for payee, names, _account, _digits_value in self._payee_index:
            if name_key in names:
                return payee
        return None

    def is_known_payee(self, name: str | None, account: str | None) -> bool:
        return self.match_payee(name, account) is not None


def load_registry(path: Path) -> Registry:
    return Registry.load(path)


__all__ = ["Registry", "load_registry", "normalize_text"]
