"""Backend layer: one narrow interface over Cohere, with the reliability rules
in one place.

Chat, Embed and Rerank all go through `_with_retries`, which owns the per-call
timeout, the retry policy (429 and 5xx only), the exponential backoff, the rate
limit and the audit line. The agent never sees an HTTP detail.

`FakeBackend` in fake.py implements the same interface, so the whole pipeline
runs offline in tests and in the demo.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from .audit import AuditLog, input_hash
from .config import EMBED_DIMENSION, Settings

RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
RATE_LIMIT_WINDOW_SECONDS = 60.0


class BackendError(RuntimeError):
    """A model call failed for good. The run cannot continue."""

    def __init__(self, message: str, *, attempts: int = 0) -> None:
        super().__init__(message)
        self.attempts = attempts


@dataclass(frozen=True)
class ToolCallRequest:
    """One tool call the model asked for, before argument validation."""

    id: str
    name: str
    arguments_raw: str


@dataclass(frozen=True)
class LLMResponse:
    """One model reply, reduced to what the agent and the metrics need.

    Per-attempt latency and retry count are not here: the audit trail owns them.
    """

    text: str | None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    tool_plan: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""


@dataclass(frozen=True)
class RerankResult:
    index: int
    score: float


class RateLimiter:
    """Sliding-window limiter, default 15 requests per minute.

    The clock and the sleep function are injectable so tests can prove the
    waiting arithmetic without spending real seconds.
    """

    def __init__(
        self,
        per_minute: int,
        *,
        clock: Any = time.monotonic,
        sleep: Any = time.sleep,
    ) -> None:
        self.per_minute = int(per_minute)
        self._clock = clock
        self._sleep = sleep
        self._calls: list[float] = []
        self.waits: list[float] = []

    def acquire(self) -> float:
        """Block until a slot is free. Returns the seconds spent waiting."""
        if self.per_minute <= 0:
            return 0.0
        waited = 0.0
        now = self._clock()
        self._calls = [stamp for stamp in self._calls if now - stamp < RATE_LIMIT_WINDOW_SECONDS]
        if len(self._calls) >= self.per_minute:
            wait = RATE_LIMIT_WINDOW_SECONDS - (now - self._calls[0])
            if wait > 0:
                self._sleep(wait)
                waited += wait
                now = self._clock()
                self._calls = [
                    stamp for stamp in self._calls if now - stamp < RATE_LIMIT_WINDOW_SECONDS
                ]
        self._calls.append(self._clock())
        self.waits.append(waited)
        return waited


class Backend(Protocol):
    """What the agent needs from a model provider."""

    name: str

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        temperature: float = 0.0,
    ) -> LLMResponse: ...

    def embed(
        self,
        *,
        model: str,
        texts: list[str],
        input_type: str,
        output_dimension: int | None = None,
    ) -> list[list[float]]: ...

    def rerank(
        self,
        *,
        model: str,
        query: str,
        documents: list[str],
        top_n: int,
    ) -> list[RerankResult]: ...


def backoff_seconds(base: float, attempt: int) -> float:
    """Exponential backoff for retry number `attempt` (1-based).

    No jitter: this runs one process at a time, so the arithmetic stays exact
    and the tests stay deterministic.
    """
    return base * float(2 ** (attempt - 1))


class CohereBackend:
    """Cohere ClientV2 with timeout, retries, rate limit and audit."""

    name = "cohere"

    def __init__(
        self,
        client: Any,
        settings: Settings,
        *,
        audit: AuditLog | None = None,
        limiter: RateLimiter | None = None,
        sleep: Any = time.sleep,
        clock: Any = time.monotonic,
    ) -> None:
        self.client = client
        self.settings = settings
        self.audit = audit
        self.limiter = limiter or RateLimiter(settings.requests_per_minute)
        self._sleep = sleep
        self._clock = clock

    def with_audit(self, audit: AuditLog | None) -> CohereBackend:
        """Same client and limiter, new audit sink. One sink per run."""
        return CohereBackend(
            self.client,
            self.settings,
            audit=audit,
            limiter=self.limiter,
            sleep=self._sleep,
            clock=self._clock,
        )

    # -- transport -------------------------------------------------------

    def _request_options(self) -> dict[str, Any]:
        return {"timeout": self.settings.timeout_seconds, "max_retries": 0}

    def _with_retries(self, kind: str, model: str, call: Any, hash_of: str | None) -> Any:
        attempts = self.settings.max_retries + 1
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            self.limiter.acquire()
            started = self._clock()
            try:
                response = call()
            except Exception as exc:  # noqa: BLE001 - classified just below
                latency_ms = int((self._clock() - started) * 1000)
                status = _status_of(exc)
                retryable = _is_retryable(exc)
                if self.audit is not None:
                    self.audit.model_call(
                        model=model,
                        attempt=attempt,
                        latency_ms=latency_ms,
                        status="error",
                        input_hash_value=hash_of,
                        error=f"{type(exc).__name__}: {status if status else exc}",
                        operation=kind,
                    )
                last_error = exc
                if not retryable or attempt >= attempts:
                    raise BackendError(
                        f"{kind} failed after {attempt} attempt(s) on {model}: {exc}",
                        attempts=attempt,
                    ) from exc
                self._sleep(backoff_seconds(self.settings.retry_backoff_seconds, attempt))
            else:
                latency_ms = int((self._clock() - started) * 1000)
                if self.audit is not None:
                    tokens_in, tokens_out = _usage_of(response)
                    self.audit.model_call(
                        model=model,
                        attempt=attempt,
                        latency_ms=latency_ms,
                        status="ok",
                        tokens_in=tokens_in,
                        tokens_out=tokens_out,
                        input_hash_value=hash_of,
                        operation=kind,
                    )
                return response
        raise BackendError(f"{kind} failed on {model}: {last_error}", attempts=attempts)

    # -- operations ------------------------------------------------------

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        temperature: float = 0.0,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "request_options": self._request_options(),
        }
        if tools:
            kwargs["tools"] = tools
        if response_format:
            kwargs["response_format"] = response_format

        hash_of = input_hash(str(messages[-1].get("content", ""))[-2000:])
        response = self._with_retries(
            "chat", model, lambda: self.client.chat(**kwargs), hash_of
        )
        message = getattr(response, "message", None)
        raw_calls = list(getattr(message, "tool_calls", None) or [])
        tool_calls = [
            ToolCallRequest(
                id=str(getattr(call, "id", "") or ""),
                name=str(getattr(getattr(call, "function", None), "name", "") or ""),
                arguments_raw=str(
                    getattr(getattr(call, "function", None), "arguments", "") or "{}"
                ),
            )
            for call in raw_calls
        ]
        tokens_in, tokens_out = _usage_of(response)
        return LLMResponse(
            text=_text_of(getattr(message, "content", None)),
            tool_calls=tool_calls,
            tool_plan=getattr(message, "tool_plan", None),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=model,
        )

    def embed(
        self,
        *,
        model: str,
        texts: list[str],
        input_type: str,
        output_dimension: int | None = None,
    ) -> list[list[float]]:
        payload = list(texts)
        response = self._with_retries(
            "embed",
            model,
            lambda: self.client.embed(
                model=model,
                texts=payload,
                input_type=input_type,
                embedding_types=["float"],
                output_dimension=output_dimension or EMBED_DIMENSION,
                request_options=self._request_options(),
            ),
            input_hash(payload[0]) if payload else None,
        )
        vectors = getattr(getattr(response, "embeddings", None), "float_", None)
        return [list(vector) for vector in (vectors or [])]

    def rerank(
        self,
        *,
        model: str,
        query: str,
        documents: list[str],
        top_n: int,
    ) -> list[RerankResult]:
        payload = list(documents)
        response = self._with_retries(
            "rerank",
            model,
            lambda: self.client.rerank(
                model=model,
                query=query,
                documents=payload,
                top_n=top_n,
                request_options=self._request_options(),
            ),
            input_hash(query),
        )
        return [
            RerankResult(index=int(item.index), score=float(item.relevance_score))
            for item in (getattr(response, "results", None) or [])
        ]

    def list_models(self) -> list[dict[str, Any]]:
        """Ask the API which models the key can use. Used by `mailroom models`."""
        response = self._with_retries("models", "models.list", self.client.models.list, None)
        models = getattr(response, "models", None) or []
        out: list[dict[str, Any]] = []
        for model in models:
            out.append(
                {
                    "name": getattr(model, "name", ""),
                    "endpoints": list(getattr(model, "endpoints", None) or []),
                    "context_length": getattr(model, "context_length", None),
                }
            )
        return out


def _status_of(exc: Exception) -> int | None:
    status = getattr(exc, "status_code", None)
    return int(status) if isinstance(status, int) else None


def _is_retryable(exc: Exception) -> bool:
    status = _status_of(exc)
    if status is not None:
        return status in RETRYABLE_STATUS
    return isinstance(exc, httpx.HTTPError)


def _text_of(content: Any) -> str | None:
    """Join the text blocks of a v2 chat message; the API returns a list, not a string."""
    if content is None or isinstance(content, str):
        return content
    parts = [getattr(block, "text", None) for block in content]
    text = "".join(p for p in parts if p)
    return text or None


def _usage_of(response: Any) -> tuple[int, int]:
    usage = getattr(response, "usage", None)
    tokens = getattr(usage, "tokens", None)
    if tokens is None:
        billed = getattr(usage, "billed_units", None)
        return (
            int(getattr(billed, "input_tokens", 0) or 0),
            int(getattr(billed, "output_tokens", 0) or 0),
        )
    return (
        int(getattr(tokens, "input_tokens", 0) or 0),
        int(getattr(tokens, "output_tokens", 0) or 0),
    )


__all__ = [
    "RETRYABLE_STATUS",
    "Backend",
    "BackendError",
    "CohereBackend",
    "LLMResponse",
    "RateLimiter",
    "RerankResult",
    "ToolCallRequest",
    "backoff_seconds",
]
