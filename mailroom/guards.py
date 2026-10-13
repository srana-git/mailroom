"""Deterministic guards.

These run on plain Python, on every letter and every proposed action, no matter
what the model said. They are the part of the safety story that a prompt cannot
undo:

* `scan_injection` looks for instructions addressed to the agent inside a letter.
* `scan_phishing` looks for the fraud patterns a letter shows: a payment to an
  account the registry does not know, a credential request, a look-alike sender
  domain, pressure to skip verification, an urgent demand for company documents,
  a threat of arrest or seizure.
* `detect_body_language` reads the language of the body, not of the letterhead.
* `check_action` blocks a payment or a send whose destination is not an approved
  registry payee.

All four are deliberately dumb and auditable. A guard that needs a model to
explain itself is not a guard.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .models import PAYEE_KINDS, WRITE_KINDS, ActionKind, GuardVerdict
from .registry import Registry, normalize_text

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


# -- language of the body --------------------------------------------------
#
# A letterhead and a signature block are printed in the language of the agency.
# The body carries the language of the sentences, and that is what has to be
# reported. Function words are counted rather than nouns, so the count follows
# the grammar of the letter and not its subject.

_FRENCH_FUNCTION_WORDS = frozenset(
    "le les la des du de une un et est sont ete etait pour vous votre vos nous notre nos "
    "dans sur avec par aux ce cet cette ces que qui dont selon entre avant apres lors "
    "ainsi tous tout toute toutes son sa ses ne pas ou aucune aucun leur leurs il elle "
    "ils elles doit peut faire etre deja sommes toutefois demeure veuillez".split()
)
_ENGLISH_FUNCTION_WORDS = frozenset(
    "the of and to in for is are you your we our that this with by on at be as it not "
    "have has from or an will would shall must was were been which when if any all may "
    "please dear".split()
)

_LANGUAGE_MIN_HITS = 4
_LANGUAGE_RATIO = 0.65
_LETTERHEAD_LINES = 2
_SIGNATURE_LINES = 2


@dataclass(frozen=True)
class LanguageVerdict:
    """The language the body is written in, and the counts behind it."""

    language: str
    french_hits: int
    english_hits: int

    @property
    def total_hits(self) -> int:
        return self.french_hits + self.english_hits

    @property
    def confidence(self) -> float:
        if not self.total_hits:
            return 0.0
        return max(self.french_hits, self.english_hits) / self.total_hits


def body_text(text: str) -> str:
    """The letter body: the letterhead block and the signature block removed.

    A letter is a run of blocks separated by blank lines: the letterhead first,
    the signature last, the body between them. When a letter has too few blocks
    to tell them apart, the first and the last two lines are dropped instead.
    """
    raw = str(text).strip()
    blocks = [block for block in re.split(r"\n\s*\n", raw) if block.strip()]
    if len(blocks) >= 3:
        return "\n".join(blocks[1:-1])
    lines = [line for line in raw.splitlines() if line.strip()]
    if len(lines) > 2 * (_LETTERHEAD_LINES + _SIGNATURE_LINES):
        return "\n".join(lines[_LETTERHEAD_LINES:-_SIGNATURE_LINES])
    return "\n".join(lines)


def detect_body_language(text: str) -> LanguageVerdict | None:
    """Read the language of the body, or None when the body is not clear.

    At least four one-language function words must be present, and one language
    must hold at least 65 percent of them. Anything weaker returns None, and the
    model's reading stands rather than being overridden on thin evidence.
    """
    words = normalize_text(body_text(text)).split()
    french = sum(1 for word in words if word in _FRENCH_FUNCTION_WORDS)
    english = sum(1 for word in words if word in _ENGLISH_FUNCTION_WORDS)
    verdict = LanguageVerdict("", french, english)
    if verdict.total_hits < _LANGUAGE_MIN_HITS or verdict.confidence < _LANGUAGE_RATIO:
        return None
    return LanguageVerdict("fr" if french >= english else "en", french, english)


# -- phishing signals ------------------------------------------------------
#
# Patterns the prompt should name, kept as plain regexes in one place. They run
# on the whole letter for phishing: a phishing letter uses its letterhead as the
# disguise, so the body alone would throw away the evidence.

SIGNAL_UNKNOWN_ACCOUNT = "payment to an account the registry does not know"
SIGNAL_PAYMENT_CHANNEL = "payment by gift card, cryptocurrency or wire"
SIGNAL_CREDENTIALS = "request for a password, a code or identity documents"
SIGNAL_DOMAIN = "sender domain that imitates an agency or uses a consumer mailbox"
SIGNAL_SKIP_VERIFICATION = "pressure to skip independent verification"
SIGNAL_URGENT_DOCUMENTS = "urgent demand for company documents"
SIGNAL_THREAT = "threat of arrest, seizure or immediate legal action"

_CONSUMER_MAIL_DOMAINS = (
    "gmail.",
    "yahoo.",
    "hotmail.",
    "outlook.",
    "proton.",
    "aol.",
    "live.",
    "icloud.",
    "gmx.",
)
_AGENCY_KEYWORDS = (
    "revenu",
    "registraire",
    "registrar",
    "arc",
    "cra",
    "cnesst",
    "impot",
    "tax",
    "ministere",
    "ministry",
    "hydro",
    "remboursement",
    "refund",
    "billing",
    "fiscal",
)
_LOOKALIKE_QUALIFIERS = (
    "secure",
    "securite",
    "security",
    "verif",
    "verification",
    "support",
    "service",
    "refund",
    "remboursement",
    "remb",
    "billing",
    "depot",
    "claim",
    "express",
    "desk",
    "alert",
    "update",
    "urgent",
    "help",
)

_EMAIL = re.compile(r"[^\s<>()\[\],;:]+@[^\s<>()\[\],;:]+")
_ACCOUNT_NUMBER = re.compile(r"\d[\d\s\-]{5,20}\d")
_CUE_WINDOW = 70

_CREDENTIAL_PATTERNS = (
    r"\bpassword\b",
    r"\bmot de passe\b",
    r"\bcode d acces\b",
    r"\baccess code\b",
    r"\bcode de verification\b",
    r"\bverification code\b",
    r"\bsocial insurance number\b",
    r"\bnumero d assurance sociale\b",
    r"\bcard number\b",
    r"\bnumero de carte\b",
    r"\bcopie de (votre )?(passeport|permis de conduire)\b",
    r"\bscan of your (id|passport)\b",
)
_DOCUMENT_PATTERNS = (
    r"\barticles of incorporation\b",
    r"\bstatuts de constitution\b",
    r"\blist of directors\b",
    r"\bliste des administrateurs\b",
    r"\bpayroll records\b",
    r"\bdossiers de paie\b",
    r"\bposte de paie\b",
    r"\bbank statements\b",
    r"\breleves bancaires\b",
    r"\bcancelled cheque\b",
    r"\bcheque annule\b",
    r"\bspecimen de cheque\b",
    r"\bidentity documents\b",
    r"\bpieces d identite\b",
)
_URGENT_TIME = (
    r"\b(24|48)\s*(heures|hours)\b",
    r"\bimmediatement\b",
    r"\bimmediately\b",
    r"\bajourd hui\b",
    r"\bdes aujourd hui\b",
    r"\bsans delai\b",
    r"\bwithin (24|48) hours\b",
)
_THREAT_PATTERNS = (
    r"\barrestation\b",
    r"\barrest\b",
    r"\bhuissier\b",
    r"\bbailiff\b",
    r"\bsaisie\b",
    r"\bseizure\b",
    r"\bperquisition\b",
    r"\bemprisonnement\b",
    r"\bimprisonment\b",
)
_SKIP_VERIFICATION_PATTERNS = (
    r"\b(do not|don t|please do not)\s+(call|telephone|phone|contact)\b",
    r"\bne (pas )?(appeler|telephoner|contacter)\b",
    r"\bn appelez pas\b",
    r"\bsans appeler\b",
)
_VERIFY_WORDS = r"\b(confirm|confirmer|verif\w*|check|valider|validate)\b"
_PAYMENT_CHANNEL_PATTERNS = (
    r"\bgift cards?\b",
    r"\bcartes? cadeaux?\b",
    r"\bbitcoin\b",
    r"\bcrypto\w*\b",
    r"\bwire transfer\b",
    r"\bvirement\b",
    r"\bmoneygram\b",
    r"\bwestern union\b",
    r"\bmandat cash\b",
)
# A destination phrase, near the number it introduces. "Compte :" alone is not
# here on purpose: it labels an account being reported, not an account to pay.
_DESTINATION_CUES = (
    r"\bpayable to\b",
    r"\bpay to\b",
    r"\bverser a\b",
    r"\bverse a\b",
    r"\ba l ordre de\b",
    r"\bcompte bancaire\b",
    r"\bbank account\b",
    r"\bnew account\b",
    r"\bnouveau compte\b",
    r"\bbeneficiary\b",
    r"\bbeneficiaire\b",
    r"\bau profit de\b",
    r"\bwire to\b",
    r"\bvirement a\b",
)

# The security procedure each signal makes mandatory. See `security_procedures`.
_SIGNAL_PROCEDURES: dict[str, str] = {
    SIGNAL_UNKNOWN_ACCOUNT: "PB-SEC-003",
    SIGNAL_SKIP_VERIFICATION: "PB-SEC-003",
    SIGNAL_URGENT_DOCUMENTS: "PB-SEC-004",
}


def _has_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


def _lookalike_domain(text: str) -> bool:
    """True when a sender domain imitates an agency or is a consumer mailbox."""
    for address in _EMAIL.findall(text):
        domain = address.rsplit("@", 1)[-1].lower()
        if any(consumer in domain for consumer in _CONSUMER_MAIL_DOMAINS):
            return True
        imitates = any(word in domain for word in _AGENCY_KEYWORDS)
        dressed_up = any(word in domain for word in _LOOKALIKE_QUALIFIERS)
        if imitates and dressed_up:
            return True
    return False


def _unknown_destination_accounts(text: str, registry: Registry | None) -> list[str]:
    """Accounts presented as a place to pay that the registry does not hold."""
    if registry is None:
        return []
    found: list[str] = []
    for match in _ACCOUNT_NUMBER.finditer(text):
        digits = "".join(ch for ch in match.group(0) if ch.isdigit())
        if not 7 <= len(digits) <= 16:
            continue
        window = normalize_text(text[max(0, match.start() - _CUE_WINDOW) : match.start()])
        if not _has_any(window, _DESTINATION_CUES):
            continue
        if registry.match_payee(None, digits) is not None:
            continue
        found.append(match.group(0).strip(" -"))
    return found


def scan_phishing(text: str, registry: Registry | None = None) -> list[str]:
    """Return the distinct fraud signals a letter shows. Empty means clean.

    The registry is what makes the first signal possible: a payment to an account
    the company has not approved is the most expensive letter in the pile. Pass
    None and that one signal is skipped; the rest are text-only.
    """
    found: list[str] = []

    def add(label: str) -> None:
        if label not in found:
            found.append(label)

    lowered = normalize_text(text)
    if _unknown_destination_accounts(text, registry):
        add(SIGNAL_UNKNOWN_ACCOUNT)
    if _has_any(lowered, _PAYMENT_CHANNEL_PATTERNS):
        add(SIGNAL_PAYMENT_CHANNEL)
    if _has_any(lowered, _CREDENTIAL_PATTERNS):
        add(SIGNAL_CREDENTIALS)
    if _lookalike_domain(text):
        add(SIGNAL_DOMAIN)
    if _has_any(lowered, _SKIP_VERIFICATION_PATTERNS) and re.search(_VERIFY_WORDS, lowered):
        add(SIGNAL_SKIP_VERIFICATION)
    if _has_any(lowered, _DOCUMENT_PATTERNS) and _has_any(lowered, _URGENT_TIME):
        add(SIGNAL_URGENT_DOCUMENTS)
    if _has_any(lowered, _THREAT_PATTERNS) and _has_any(lowered, _URGENT_TIME):
        add(SIGNAL_THREAT)
    return found


def security_procedures(
    injection_flags: list[str], phishing_signals: list[str]
) -> list[str]:
    """The procedures the deterministic scans require in the retrieved set.

    Retrieval is scored on the top three procedures. The specific procedures are
    listed before the general one, so a forced general procedure cannot push a
    forced specific one out of the scored window.
    """
    ids: list[str] = []

    def add(value: str) -> None:
        if value not in ids:
            ids.append(value)

    if injection_flags:
        add("PB-SEC-002")
    if phishing_signals:
        for signal in phishing_signals:
            mapped = _SIGNAL_PROCEDURES.get(signal)
            if mapped:
                add(mapped)
        add("PB-SEC-001")
    return ids


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


__all__ = [
    "SIGNAL_CREDENTIALS",
    "SIGNAL_DOMAIN",
    "SIGNAL_PAYMENT_CHANNEL",
    "SIGNAL_SKIP_VERIFICATION",
    "SIGNAL_THREAT",
    "SIGNAL_UNKNOWN_ACCOUNT",
    "SIGNAL_URGENT_DOCUMENTS",
    "LanguageVerdict",
    "body_text",
    "check_action",
    "detect_body_language",
    "extract_destination",
    "scan_injection",
    "scan_phishing",
    "security_procedures",
]
