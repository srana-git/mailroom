"""Deterministic guards.

These run on plain Python, on every letter and every proposed action, no matter
what the model said. They are the part of the safety story that a prompt cannot
undo:

* `scan_injection` looks for instructions addressed to the agent inside a letter.
* `check_action` blocks a payment or a send whose destination is not an approved
  registry payee.

Both are deliberately dumb and auditable. A guard that needs a model to explain
itself is not a guard.
"""

from __future__ import annotations

import re
from typing import Any

from .models import PAYEE_KINDS, WRITE_KINDS, ActionKind, GuardVerdict
from .registry import Registry

# Patterns that indicate the letter body is trying to instruct the agent rather
# than inform a human. French and English. Kept as plain regexes so a reviewer
# can read every rule.
_INJECTION_RULES: tuple[tuple[str, str], ...] = (
    (r"ignore\s+(all\s+|any\s+)?(the\s+)?(previous|prior|above|earlier|your)\s+(instruction|rule|prompt|direction)",
     "english instruction-override wording"),
    (r"disregard\s+(all\s+|any\s+)?(the\s+)?(previous|prior|above|earlier|your)\s+(instruction|rule|prompt|direction)",
     "english instruction-override wording"),
    (r"ignorez\s+(toutes?\s+)?(les\s+)?(instructions|r[eè]gles|consignes)",
     "french instruction-override wording"),
    (r"ne\s+tenez\s+pas\s+compte\s+des\s+(instructions|r[eè]gles)",
     "french instruction-override wording"),
    (r"\b(system|syst[eè]me)\s+prompt\b", "prompt-extraction wording"),
    (r"(reveal|print|show|repeat|output)\s+(your\s+)?(instruction|system prompt|prompt)",
     "prompt-extraction wording"),
    (r"(r[eé]v[eé]lez|affichez|montrez)\s+(vos\s+)?(instructions|consignes|prompt)",
     "prompt-extraction wording"),
    (r"you\s+are\s+now\s+", "role-change wording"),
    (r"vous\s+[eê]tes\s+(maintenant|d[eé]sormais)\s+", "role-change wording"),
    (r"as\s+an?\s+(ai|assistant|model|language\s+model)", "role-change wording"),
    (r"en\s+tant\s+qu['’]?\s*(assistant|ia|mod[eè]le)", "role-change wording"),
    (r"new\s+instructions?\s*:", "role-change wording"),
    (r"nouvelles?\s+instructions?\s*:", "role-change wording"),
    (r"do\s+not\s+(tell|inform|alert|notify)\s+(the\s+)?(user|human|operator|client)",
     "concealment wording"),
    (r"ne\s+(dites|informez|avertissez|avisez)\s+pas\s+(l['’]utilisateur|le\s+client|un\s+humain)",
     "concealment wording"),
    (r"forward\s+(all\s+|the\s+|our\s+)*(record|data|file|document|list|contact|payroll)",
     "data-exfiltration wording"),
    (r"(transmettez|envoyez)\s+(tous\s+|toutes\s+)?(les\s+)?(dossiers|donn[eé]es|documents|fichiers|coordonn[eé]es)",
     "data-exfiltration wording"),
    (r"(pay|wire|send|transfer|remit)\s+(the\s+)?(payment|funds|amount|money)?\s*(to|into|at)\s+(a\s+|the\s+)?(new|different|updated|alternate|other)",
     "payment-destination-change wording"),
    (r"(payez|virez|transf[eé]rez|envoyez)\s+(le\s+)?(paiement|montant|fonds)?\s*(à|a|au|vers)\s+(un\s+|le\s+)?(nouveau|autre|diff[eé]rent)",
     "payment-destination-change wording"),
    (r"override\s+(the\s+)?(guard|rule|policy|check|approval)",
     "guard-override wording"),
    (r"(contournez|ignorez)\s+(le\s+)?(contr[oô]le|garde|filtre|s[eé]curit[eé])",
     "guard-override wording"),
    (r"mark\s+(it|this)\s+as\s+(paid|approved|complete)",
     "status-falsification wording"),
    (r"marquez?\s+(le|ce|cette)\s+(comme\s+)?(pay[eé]|approuv[eé]|trait[eé])",
     "status-falsification wording"),
)

_COMPILED: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), label) for pattern, label in _INJECTION_RULES
)

_PAYEE_KEYS = ("payee", "payee_name", "beneficiary", "recipient", "to", "merchant")
_ACCOUNT_KEYS = (
    "account",
    "account_number",
    "bank_account",
    "iban",
    "destination_account",
    "routing_number",
)


def scan_injection(text: str) -> list[str]:
    """Return the distinct rule labels a letter trips. Empty means clean."""
    found: list[str] = []
    for pattern, label in _COMPILED:
        if pattern.search(text) and label not in found:
            found.append(label)
    return found


def extract_destination(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    """Pull (payee, account) out of a proposed action payload."""
    payee: str | None = None
    account: str | None = None
    for key in _PAYEE_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            payee = value.strip()
            break
    for key in _ACCOUNT_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            account = value.strip()
            break
        if isinstance(value, (int, float)):
            account = str(value)
            break
    return payee, account


def check_action(
    kind: ActionKind,
    payload: dict[str, Any],
    registry: Registry,
) -> GuardVerdict:
    """Decide whether one proposed action may go to a human as an approvable item.

    A blocked action is still queued, flagged as blocked, so a human can see what
    the agent tried to do. It is never approvable as-is.
    """
    if kind not in WRITE_KINDS:
        return GuardVerdict(allowed=True, reasons=[])

    if kind not in PAYEE_KINDS:
        return GuardVerdict(allowed=True, reasons=[])

    payee, account = extract_destination(payload)
    if not payee and not account:
        return GuardVerdict(
            allowed=False,
            reasons=[f"{kind.value} action names no payee and no account"],
        )
    # The account decides when one is present. See Registry.match_payee.

    match = registry.match_payee(payee, account)
    if match is None:
        described = " / ".join(part for part in (payee, account) if part)
        return GuardVerdict(
            allowed=False,
            reasons=[
                f"{kind.value} destination '{described}' is not a registry payee",
            ],
        )

    return GuardVerdict(
        allowed=True,
        reasons=[f"destination matches registry payee '{match['name']}'"],
    )


__all__ = ["check_action", "extract_destination", "scan_injection"]
