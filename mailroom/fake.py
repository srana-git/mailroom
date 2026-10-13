"""An offline stand-in for Cohere.

Purpose: let the whole pipeline, the tests and the demo run with no API key and
no network. It is a small rule-based reader, not a language model, and it is
never used to produce eval numbers -- `mailroom eval` refuses to run on it.

Two modes:

* Heuristic (default). Reads the letter, then asks for one tool at a time in a
  fixed order, which is enough to drive the agent loop end to end.
* Scripted (`script=[...]`). Each element is a tool call dict, a plain string,
  or an Exception to raise. Tests use this to pin exact behaviour: a bad
  argument, a retry, a refusal.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from .config import EMBED_DIMENSION
from .llm import LLMResponse, RerankResult, ToolCallRequest
from .models import REQUIRED_ACTION_TO_KIND, LetterType, RequiredAction
from .registry import normalize_text

MONTHS_FR = {
    "janvier": 1, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11,
    "decembre": 12,
}
MONTHS_EN = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

FRENCH_MARKERS = (
    "vous", "votre", "nous", "montant", "echeance", "avant le", "veuillez",
    "dossier", "objet", "sommes", "teneur", "reglement", "svp",
)
SUSPICIOUS_MARKERS = (
    "carte-cadeau", "gift card", "carte cadeau", "bitcoin", "crypto", "mot de passe",
    "password", "code d'acces", "acces au compte", "cliquez ici", "click here",
    "48 heures", "48 hours", "24 heures", "24 hours", "compte different",
    "different account", "nouveau compte", "new account",
)
PAYEE_PATTERNS = (
    r"(?:à l['’]ordre de|a l['’]ordre de|au profit de|payable to|payable au profit de|"
    r"beneficiaire\s*:|bénéficiaire\s*:|payee\s*:|pay to|verser à|verser a)\s*([^\n,;.]{3,70})",
)
ACCOUNT_PATTERNS = (
    r"(?:compte(?:\s+bancaire)?|account|iban|num[eé]ro de compte)\s*(?:n[o°]?\.?\s*)?[:#]?\s*"
    r"([0-9][0-9\-\s]{4,30})",
)
COMPANY_PATTERN = (
    r"\b([A-ZÉÈÀÂÎÔÛÇ][\w'’\-]+(?:\s+[A-ZÉÈÀÂÎÔÛÇ][\w'’\-]+){0,3}\s+"
    r"(?:Inc\.|Inc|Lt[eé]e|SENC|S\.E\.N\.C\.|Corp\.|Corp|Limit[eé]e))"
)
NEQ_PATTERN = r"\b(1199999\d{3})\b"
REFERENCE_PATTERNS = (
    r"(?:r[eé]f[eé]rence|reference|no\.?\s*de\s*dossier|dossier|n[o°]\s*de\s*compte|"
    r"case\s*number|account\s*number|compte)\s*(?:n[o°]?\.?)?\s*[:#]?\s*([A-Z0-9][A-Z0-9\-/]{3,24})",
)

AMOUNT_PATTERN = r"([0-9][0-9\s .,]*[0-9]|[0-9])\s*(?:\$|CA\$|CAD|USD|EUR)"


@dataclass
class _FieldGuess:
    fields: dict[str, Any]


class FakeBackend:
    """Implements the Backend protocol without any network call."""

    name = "fake"

    def __init__(
        self,
        *,
        dimension: int = EMBED_DIMENSION,
        script: list[Any] | None = None,
        tokens_in: int = 1200,
        tokens_out: int = 180,
    ) -> None:
        self.dimension = dimension
        self.script = list(script) if script is not None else None
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.calls: list[dict[str, Any]] = []

    # -- chat ------------------------------------------------------------

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        temperature: float = 0.0,
    ) -> LLMResponse:
        self.calls.append({"model": model, "tools": bool(tools), "format": response_format})
        if response_format and response_format.get("type") == "json_object":
            return self._plan_response(model, messages)

        if self.script is not None:
            return self._scripted_response(model)

        if not tools:
            return LLMResponse(
                text=self._summary(messages),
                tokens_in=self.tokens_in,
                tokens_out=self.tokens_out,
                model=model,
            )
        return self._heuristic_response(model, messages)

    def _scripted_response(self, model: str) -> LLMResponse:
        step = self.script.pop(0) if self.script else "done"
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, str):
            return self._reply(step, model)
        if step == "done" or step is None:
            return self._reply("done", model)
        name = step["tool"]
        arguments = step.get("args", {})
        raw = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
        return LLMResponse(
            text=None,
            tool_calls=[
                ToolCallRequest(
                    id=f"call_{name}_{len(self.calls)}", name=name, arguments_raw=raw
                )
            ],
            tool_plan=f"scripted call to {name}",
            tokens_in=self.tokens_in,
            tokens_out=self.tokens_out,
            model=model,
        )

    def _reply(self, text: str, model: str) -> LLMResponse:
        return LLMResponse(
            text=text,
            tokens_in=self.tokens_in,
            tokens_out=self.tokens_out,
            model=model,
        )

    def _plan_response(self, model: str, messages: list[dict[str, Any]]) -> LLMResponse:
        plan = {
            "steps": [
                "extract_fields to read issuer, type, amount, due date and urgency",
                "search_playbook for the procedure that matches the letter type",
                "lookup_company to identify which of our companies the letter names",
                "propose_action to queue the next move for a human",
            ],
            "reason": "the letter must be read, matched to a procedure, then handed to a human",
        }
        return LLMResponse(
            text=json.dumps(plan, ensure_ascii=False),
            tokens_in=self.tokens_in,
            tokens_out=self.tokens_out,
            model=model,
        )

    def _heuristic_response(self, model: str, messages: list[dict[str, Any]]) -> LLMResponse:
        letter = self._letter_text(messages)
        called = self._tools_called(messages)
        guess = extract_guess(letter)

        if "extract_fields" not in called:
            return self._tool(model, "extract_fields", guess.fields)

        if "search_playbook" not in called:
            query = " ".join(
                (
                    str(guess.fields["letter_type"]),
                    str(guess.fields["issuer"]),
                    str(guess.fields["required_action"]),
                )
            )
            return self._tool(model, "search_playbook", {"query": query, "top_k": 3})

        if "lookup_company" not in called:
            company_query = find_company_query(letter) or guess.fields["issuer"]
            return self._tool(model, "lookup_company", {"query": company_query})

        if "propose_action" not in called:
            return self._tool(model, "propose_action", build_action(letter, guess.fields))

        return LLMResponse(
            text=f"Read as {guess.fields['letter_type']}; next move queued for a human.",
            tokens_in=self.tokens_in,
            tokens_out=self.tokens_out,
            model=model,
        )

    def _tool(self, model: str, name: str, args: dict[str, Any]) -> LLMResponse:
        return LLMResponse(
            text=None,
            tool_calls=[
                ToolCallRequest(
                    id=f"call_{name}_{len(self.calls)}",
                    name=name,
                    arguments_raw=json.dumps(args, ensure_ascii=False, default=str),
                )
            ],
            tool_plan=f"call {name}",
            tokens_in=self.tokens_in,
            tokens_out=self.tokens_out,
            model=model,
        )

    @staticmethod
    def _letter_text(messages: list[dict[str, Any]]) -> str:
        for message in messages:
            if message.get("role") == "user":
                return str(message.get("content", ""))
        return ""

    @staticmethod
    def _tools_called(messages: list[dict[str, Any]]) -> set[str]:
        names: set[str] = set()
        for message in messages:
            for call in message.get("tool_calls") or []:
                function = call.get("function") if isinstance(call, dict) else None
                if function and function.get("name"):
                    names.add(str(function["name"]))
        return names

    def _summary(self, messages: list[dict[str, Any]]) -> str:
        return "Triage complete."

    # -- embed -----------------------------------------------------------

    def embed(
        self,
        *,
        model: str,
        texts: list[str],
        input_type: str,
        output_dimension: int | None = None,
    ) -> list[list[float]]:
        dimension = output_dimension or self.dimension
        return [hash_embedding(text, dimension) for text in texts]

    # -- rerank ----------------------------------------------------------

    def rerank(
        self,
        *,
        model: str,
        query: str,
        documents: list[str],
        top_n: int,
    ) -> list[RerankResult]:
        query_terms = set(_tokens(query))
        scored: list[tuple[float, int]] = []
        for index, document in enumerate(documents):
            doc_terms = _tokens(document)
            if not doc_terms:
                scored.append((0.0, index))
                continue
            overlap = len(query_terms & set(doc_terms))
            scored.append((overlap / math.sqrt(len(doc_terms)), index))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [RerankResult(index=index, score=score) for score, index in scored[:top_n]]


def _tokens(text: str) -> list[str]:
    return [token for token in normalize_text(text).split() if len(token) > 2]


def hash_embedding(text: str, dimension: int) -> list[float]:
    """Signed hashing bag of words, L2 normalised.

    Deterministic, so tests and the demo are repeatable. Similar texts land near
    each other, which is enough to make offline retrieval behave sensibly.
    """
    vector = [0.0] * dimension
    for token in _tokens(text):
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        index = value % dimension
        vector[index] += 1.0 if (value >> 63) & 1 else -1.0
    norm = math.sqrt(sum(component * component for component in vector))
    if norm == 0.0:
        return vector
    return [component / norm for component in vector]


# -- letter reading heuristics ------------------------------------------


def detect_language(text: str) -> str:
    lowered = normalize_text(text)
    score = sum(1 for marker in FRENCH_MARKERS if normalize_text(marker) in lowered)
    return "fr" if score >= 2 else "en"


def find_company_query(text: str) -> str | None:
    match = re.search(NEQ_PATTERN, text)
    if match:
        return match.group(1)
    match = re.search(COMPANY_PATTERN, text)
    if match:
        return match.group(1).strip()
    return None


def find_amount(text: str) -> tuple[float | None, str | None]:
    for match in re.finditer(AMOUNT_PATTERN, text):
        raw = match.group(1)
        digits = re.sub(r"[^\d,.]", "", raw)
        amount = _parse_number(digits)
        if amount is None:
            continue
        symbol = match.group(0)
        if "$" in symbol:
            currency = "CAD"
        elif "EUR" in symbol.upper():
            currency = "EUR"
        elif "USD" in symbol.upper():
            currency = "USD"
        else:
            currency = "CAD"
        return amount, currency
    return None, None


def _parse_number(digits: str) -> float | None:
    if not digits:
        return None
    if "," in digits and "." in digits:
        digits = digits.replace(" ", "").replace(",", "")
    elif "," in digits:
        head, _, tail = digits.rpartition(",")
        digits = f"{head}.{tail}" if len(tail) == 2 else digits.replace(",", "")
    try:
        return round(float(digits), 2)
    except ValueError:
        return None


def find_due_date(text: str) -> date | None:
    lowered = text.casefold()
    for match in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", text):
        return _safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    for match in re.finditer(r"\b(\d{1,2})\s+([A-Za-zéûôàè]+)\s+(\d{4})\b", lowered):
        month = MONTHS_FR.get(normalize_text(match.group(2))) or MONTHS_EN.get(match.group(2))
        if month:
            return _safe_date(int(match.group(3)), month, int(match.group(1)))
    for match in re.finditer(r"\b([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})\b", lowered):
        month = MONTHS_EN.get(match.group(1))
        if month:
            return _safe_date(int(match.group(3)), month, int(match.group(2)))
    for match in re.finditer(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b", text):
        return _safe_date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
    return None


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def find_reference(text: str) -> str | None:
    for pattern in REFERENCE_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            value = match.group(1).strip().strip(",;.")
            if value:
                return value
    return None


PHISHING_MARKERS = (
    "phishing",
    "hameconnage",
    "fraude",
    "fraud",
    "verification de securite",
    "verify your account",
)
FINAL_NOTICE_MARKERS = ("avis final", "final notice", "dernier avis", "mise en demeure")
REGISTRY_MARKERS = (
    "declaration annuelle",
    "annual declaration",
    "registre des entreprises",
    "registraire des entreprises",
)
PAYROLL_MARKERS = (
    "retenue",
    "remittance",
    "remise sur la paie",
    "paie",
    "payroll",
    "das",
    "cnesst",
)
INFORMATION_MARKERS = (
    "aucune action",
    "information seulement",
    "for information only",
    "no action required",
    "avis de changement",
)
PAYMENT_DEMAND_MARKERS = (
    "somme due",
    "montant du",
    "amount due",
    "paiement",
    "payment",
    "avis de cotisation",
    "notice of assessment",
)
CONTEST_MARKERS = ("contestation", "contester", "contest", "opposition", "avis d'opposition")
SEVERE_URGENCY_MARKERS = (
    "saisie",
    "huissier",
    "suspension",
    "penalite",
    "penalty",
    "interet",
)


def _has_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def classify_letter_type(text: str) -> LetterType:
    lowered = normalize_text(text)
    if _has_any(lowered, PHISHING_MARKERS):
        return LetterType.PHISHING
    if _has_any(lowered, FINAL_NOTICE_MARKERS):
        return LetterType.FINAL_NOTICE
    if _has_any(lowered, REGISTRY_MARKERS):
        return LetterType.REGISTRY_DECLARATION_REMINDER
    if _has_any(lowered, PAYROLL_MARKERS):
        return LetterType.PAYROLL_REMITTANCE
    if _has_any(lowered, INFORMATION_MARKERS):
        return LetterType.INFORMATION_ONLY
    if _has_any(lowered, PAYMENT_DEMAND_MARKERS):
        return LetterType.PAYMENT_DEMAND
    return LetterType.ROUTINE_NOTICE


NO_ACTION_MARKERS = (
    "aucune action",
    "information seulement",
    "for information only",
    "no action required",
    "no action is required",
    "aucun solde n est du",
)
PAYMENT_MARKERS = ("solde", "balance", "montant", "amount", "paiement", "payment", "exigible")


def is_contradictory(text: str) -> bool:
    """True when a letter says both "nothing to do" and "an amount is due"."""
    lowered = normalize_text(text)
    has_no_action = any(normalize_text(marker) in lowered for marker in NO_ACTION_MARKERS)
    has_payment = any(normalize_text(marker) in lowered for marker in PAYMENT_MARKERS)
    return has_no_action and has_payment


def classify_action(text: str, letter_type: LetterType) -> RequiredAction:
    lowered = normalize_text(text)
    if letter_type is LetterType.PHISHING:
        return RequiredAction.REPORT_PHISHING
    if is_contradictory(text):
        return RequiredAction.ESCALATE
    if _has_any(lowered, CONTEST_MARKERS):
        return RequiredAction.CONTEST
    if letter_type is LetterType.INFORMATION_ONLY:
        return RequiredAction.NO_ACTION
    if letter_type is LetterType.REGISTRY_DECLARATION_REMINDER:
        return RequiredAction.FILE_DECLARATION
    if letter_type is LetterType.PAYROLL_REMITTANCE:
        return RequiredAction.UPDATE_PAYROLL
    if any(marker in lowered for marker in ("adresse", "address", "changement d'adresse")):
        return RequiredAction.CONFIRM_ADDRESS
    if letter_type in (LetterType.PAYMENT_DEMAND, LetterType.FINAL_NOTICE):
        return RequiredAction.PAY
    if any(marker in lowered for marker in ("document", "piece jointe", "justificatif")):
        return RequiredAction.PROVIDE_DOCUMENTS
    return RequiredAction.ESCALATE


def classify_urgency(text: str, letter_type: LetterType, action: RequiredAction) -> int:
    lowered = normalize_text(text)
    if action is RequiredAction.NO_ACTION:
        return 1
    if letter_type is LetterType.PHISHING:
        return 3
    if action is RequiredAction.ESCALATE:
        return 3
    if letter_type is LetterType.FINAL_NOTICE or _has_any(lowered, SEVERE_URGENCY_MARKERS):
        return 5
    if letter_type is LetterType.PAYMENT_DEMAND:
        return 4
    if action is RequiredAction.FILE_DECLARATION:
        return 3
    return 2


def find_issuer(text: str) -> str:
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    match = re.search(r"^([A-ZÉÈÀÂÎÔÛÇ][^\n]{5,80})$", first_line)
    if match:
        return match.group(1).strip().rstrip(":").strip()
    return first_line or "unknown"


def find_payee(text: str) -> str | None:
    for pattern in PAYEE_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip().strip(",;.")
    return None


def find_account(text: str) -> str | None:
    for pattern in ACCOUNT_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return re.sub(r"[\s\-]", "", match.group(1))
    return None


def extract_guess(text: str) -> _FieldGuess:
    """The fake's single pass over a letter."""
    language = detect_language(text)
    letter_type = classify_letter_type(text)
    action = classify_action(text, letter_type)
    amount, currency = find_amount(text)
    due = find_due_date(text)
    lowered = normalize_text(text)
    suspicious = letter_type is LetterType.PHISHING or any(
        normalize_text(marker) in lowered for marker in SUSPICIOUS_MARKERS
    )
    return _FieldGuess(
        fields={
            "issuer": find_issuer(text),
            "letter_type": letter_type.value,
            "language": language,
            "reference_number": find_reference(text),
            "amount_due": amount,
            "currency": currency,
            "due_date": due.isoformat() if due else None,
            "required_action": action.value,
            "urgency": classify_urgency(text, letter_type, action),
            "is_suspicious": suspicious,
            "suspicion_reason": "spoofed sender or credential request" if suspicious else None,
        }
    )


def build_action(text: str, fields: dict[str, Any]) -> dict[str, Any]:
    """The action the fake proposes. It obeys any payee named in the letter.

    That is the point: it plays the part of a model that reads the letter as
    instructions, so the deterministic payee guard has something to catch.
    """
    required = RequiredAction(fields["required_action"])
    kind = REQUIRED_ACTION_TO_KIND[required]
    payee = find_payee(text)
    account = find_account(text)
    amount, currency = find_amount(text)

    if kind.value == "pay":
        payload = {
            "payee": payee or fields["issuer"],
            "account": account,
            "amount": amount,
            "currency": currency,
            "reference": fields["reference_number"],
            "due_date": fields["due_date"],
        }
    elif kind.value == "send":
        payload = {
            "to": payee or fields["issuer"],
            "subject": f"Re: {fields['reference_number'] or fields['issuer']}",
            "body_summary": "Confirm the requested information.",
        }
    elif kind.value in ("file", "contest"):
        payload = {
            "form": fields["required_action"],
            "reference": fields["reference_number"],
            "deadline": fields["due_date"],
        }
    elif kind.value == "escalate":
        payload = {"reason": "the letter does not settle the required action"}
    else:
        payload = {}
    return {"kind": kind.value, "payload": payload}


__all__ = [
    "FakeBackend",
    "build_action",
    "detect_language",
    "extract_guess",
    "find_account",
    "find_amount",
    "find_company_query",
    "find_due_date",
    "find_issuer",
    "find_payee",
    "find_reference",
    "hash_embedding",
]
