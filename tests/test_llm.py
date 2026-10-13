"""The backend layer: retries, timeouts, rate limiting, response normalisation."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cohere.core.api_error import ApiError

from mailroom.audit import AuditLog, read_run
from mailroom.config import Settings
from mailroom.llm import (
    BackendError,
    CohereBackend,
    RateLimiter,
    ToolCallRequest,
    backoff_seconds,
)


class FakeClock:
    """Monotonic stand-in whose sleep advances time."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def chat_response(text: str | None = "ok", tool_calls: list | None = None, tokens=(10, 5)):
    return SimpleNamespace(
        message=SimpleNamespace(
            content=text,
            tool_calls=tool_calls,
            tool_plan="plan" if tool_calls else None,
        ),
        usage=SimpleNamespace(
            tokens=SimpleNamespace(input_tokens=tokens[0], output_tokens=tokens[1])
        ),
    )


class StubClient:
    """Returns a scripted list, one item per call. A BaseException item is raised.

    Any of the client entry points the backend uses (chat, embed, rerank) draws
    from the same script, so one stub covers all three operations.
    """

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.calls = 0

    def _next(self, **_kwargs):
        item = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(item, BaseException):
            raise item
        return item

    def __getattr__(self, name: str):
        if name in ("chat", "embed", "rerank"):
            return self._next
        raise AttributeError(name)


def build_backend(settings: Settings, script: list, audit: AuditLog | None = None, clock=None):
    clock = clock or FakeClock()
    settings = Settings.build(
        root=settings.root,
        backend=settings.backend,
        state_dir=settings.state_dir,
        index_dir=settings.index_dir,
        queue_path=settings.queue_path,
        runs_dir=settings.runs_dir,
        requests_per_minute=0,
    )
    return CohereBackend(
        StubClient(script), settings, audit=audit, sleep=clock.sleep, clock=clock
    )


def test_backoff_doubles_from_the_base():
    assert backoff_seconds(0.5, 1) == 0.5
    assert backoff_seconds(0.5, 2) == 1.0
    assert backoff_seconds(0.5, 3) == 2.0


def test_a_clean_call_returns_a_normalised_response(settings: Settings):
    backend = build_backend(settings, [chat_response("hello")])
    response = backend.chat(model="m", messages=[{"role": "user", "content": "hi"}])
    assert response.text == "hello"
    assert response.tokens_in == 10
    assert response.tokens_out == 5


def test_tool_calls_are_normalised_into_dataclasses(settings: Settings):
    function = SimpleNamespace(name="extract_fields", arguments='{"a":1}')
    call = SimpleNamespace(id="c1", type="function", function=function)
    backend = build_backend(settings, [chat_response(None, [call])])
    response = backend.chat(model="m", messages=[{"role": "user", "content": "hi"}])
    assert response.tool_calls == [
        ToolCallRequest(id="c1", name="extract_fields", arguments_raw='{"a":1}')
    ]


def test_a_429_is_retried_and_then_succeeds(settings: Settings):
    backend = build_backend(
        settings, [ApiError(status_code=429, body="slow down"), chat_response("fine")]
    )
    response = backend.chat(model="m", messages=[{"role": "user", "content": "hi"}])
    assert response.text == "fine"


def test_a_5xx_is_retried_and_then_succeeds(settings: Settings):
    backend = build_backend(
        settings, [ApiError(status_code=503, body="down"), chat_response("fine")]
    )
    assert backend.chat(model="m", messages=[{"role": "user", "content": "hi"}]).text == "fine"


def test_a_400_is_not_retried(settings: Settings):
    client = StubClient([ApiError(status_code=400, body="bad request")])
    backend = CohereBackend(client, settings, sleep=lambda _s: None)
    with pytest.raises(BackendError):
        backend.chat(model="m", messages=[{"role": "user", "content": "hi"}])
    assert client.calls == 1


def test_repeated_5xx_gives_up_after_the_configured_retries(settings: Settings):
    error = ApiError(status_code=500, body="boom")
    client = StubClient([error])
    backend = CohereBackend(client, settings, sleep=lambda _s: None)
    with pytest.raises(BackendError) as excinfo:
        backend.chat(model="m", messages=[{"role": "user", "content": "hi"}])
    assert excinfo.value.attempts == settings.max_retries + 1
    assert client.calls == settings.max_retries + 1


def test_a_transport_timeout_is_retried(settings: Settings):
    backend = build_backend(
        settings, [httpx.ConnectTimeout("timed out"), chat_response("fine")]
    )
    assert backend.chat(model="m", messages=[{"role": "user", "content": "hi"}]).text == "fine"


def test_the_backoff_sleep_is_actually_taken(settings: Settings):
    error = ApiError(status_code=500, body="boom")
    clock = FakeClock()
    backend = build_backend(settings, [error, error, chat_response("fine")], clock=clock)
    backend.chat(model="m", messages=[{"role": "user", "content": "hi"}])
    assert clock.now == pytest.approx(settings.retry_backoff_seconds * 3)


def test_every_attempt_is_written_to_the_audit_trail(settings: Settings):
    directory = settings.runs_dir
    log = AuditLog("run1", directory)
    backend = build_backend(
        settings, [ApiError(status_code=429, body="slow"), chat_response("fine")], audit=log
    )
    backend.chat(model="m", messages=[{"role": "user", "content": "hi"}])
    events = read_run("run1", directory)
    assert [event["status"] for event in events] == ["error", "ok"]
    assert events[1]["retry_count"] == 1


def test_with_audit_returns_a_backend_bound_to_the_new_log(settings: Settings):
    directory = settings.runs_dir
    backend = build_backend(settings, [chat_response("fine")])
    bound = backend.with_audit(AuditLog("run9", directory))
    bound.chat(model="m", messages=[{"role": "user", "content": "hi"}])
    assert read_run("run9", directory)[0]["kind"] == "model_call"


def test_embed_returns_one_vector_per_text(settings: Settings):
    response = SimpleNamespace(embeddings=SimpleNamespace(float_=[[0.1, 0.2], [0.3, 0.4]]))
    client = StubClient([response])
    backend = CohereBackend(client, settings, sleep=lambda _s: None)
    vectors = backend.embed(model="e", texts=["a", "b"], input_type="search_document")
    assert vectors == [[0.1, 0.2], [0.3, 0.4]]


def test_rerank_returns_index_and_score(settings: Settings):
    response = SimpleNamespace(
        results=[
            SimpleNamespace(index=2, relevance_score=0.9),
            SimpleNamespace(index=0, relevance_score=0.4),
        ]
    )
    client = StubClient([response])
    backend = CohereBackend(client, settings, sleep=lambda _s: None)
    results = backend.rerank(model="r", query="q", documents=["a", "b", "c"], top_n=2)
    assert [(item.index, item.score) for item in results] == [(2, 0.9), (0, 0.4)]


def test_usage_falls_back_to_billed_units(settings: Settings):
    response = SimpleNamespace(
        message=SimpleNamespace(content="ok", tool_calls=None, tool_plan=None),
        usage=SimpleNamespace(
            tokens=None,
            billed_units=SimpleNamespace(input_tokens=7, output_tokens=3),
        ),
    )
    client = StubClient([response])
    backend = CohereBackend(client, settings, sleep=lambda _s: None)
    result = backend.chat(model="m", messages=[{"role": "user", "content": "hi"}])
    assert (result.tokens_in, result.tokens_out) == (7, 3)


def test_rate_limiter_lets_the_first_calls_through_without_waiting():
    clock = FakeClock()
    limiter = RateLimiter(2, clock=clock, sleep=clock.sleep)
    limiter.acquire()
    limiter.acquire()
    assert limiter.waits == [0.0, 0.0]


def test_rate_limiter_waits_when_the_window_is_full():
    clock = FakeClock()
    limiter = RateLimiter(2, clock=clock, sleep=clock.sleep)
    limiter.acquire()
    limiter.acquire()
    limiter.acquire()
    assert limiter.waits[-1] == pytest.approx(60.0)


def test_rate_limiter_can_be_disabled():
    clock = FakeClock()
    limiter = RateLimiter(0, clock=clock, sleep=clock.sleep)
    for _ in range(5):
        limiter.acquire()
    assert clock.now == 0.0


# -- retrieval calls are audited too ---------------------------------------


def _limited(settings: Settings, per_minute: int) -> Settings:
    return Settings.build(
        root=settings.root,
        backend=settings.backend,
        state_dir=settings.state_dir,
        index_dir=settings.index_dir,
        queue_path=settings.queue_path,
        runs_dir=settings.runs_dir,
        requests_per_minute=per_minute,
    )


def test_an_embed_call_is_logged_as_an_embed_event(settings: Settings):
    directory = settings.runs_dir
    log = AuditLog("run-embed", directory)
    response = SimpleNamespace(embeddings=SimpleNamespace(float_=[[0.1, 0.2]]))
    backend = CohereBackend(
        StubClient([response]), _limited(settings, 0), audit=log, sleep=lambda _s: None
    )
    backend.embed(model="embed-v4.0", texts=["a query"], input_type="search_query")
    events = read_run("run-embed", directory)
    assert events[0]["kind"] == "model_call"
    assert events[0]["operation"] == "embed"
    assert events[0]["model"] == "embed-v4.0"


def test_a_rerank_call_is_logged_as_a_rerank_event(settings: Settings):
    directory = settings.runs_dir
    log = AuditLog("run-rerank", directory)
    response = SimpleNamespace(results=[SimpleNamespace(index=0, relevance_score=0.5)])
    backend = CohereBackend(
        StubClient([response]), _limited(settings, 0), audit=log, sleep=lambda _s: None
    )
    backend.rerank(model="rerank-v4.0-pro", query="q", documents=["a"], top_n=1)
    assert read_run("run-rerank", directory)[0]["operation"] == "rerank"


def test_the_rate_limiter_wait_is_not_inside_the_logged_latency(settings: Settings):
    """Model time excludes the limiter wait, which is why it is reported apart."""
    clock = FakeClock()
    directory = settings.runs_dir
    log = AuditLog("run-limit", directory)
    backend = CohereBackend(
        StubClient([chat_response("one"), chat_response("two")]),
        _limited(settings, 1),
        audit=log,
        limiter=RateLimiter(1, clock=clock, sleep=clock.sleep),
        sleep=clock.sleep,
        clock=clock,
    )
    backend.chat(model="m", messages=[{"role": "user", "content": "a"}])
    backend.chat(model="m", messages=[{"role": "user", "content": "b"}])
    events = read_run("run-limit", directory)
    assert [event["latency_ms"] for event in events] == [0, 0]
    assert clock.now == pytest.approx(60.0)


def test_a_missing_index_directory_raises_a_helpful_error(tmp_path: Path):
    from mailroom.indexer import IndexError_, PlaybookIndex

    state = tmp_path / "state"
    settings = Settings.build(
        root=tmp_path, backend="fake", state_dir=state, index_dir=state / "index"
    )
    index = PlaybookIndex(settings, None)
    with pytest.raises(IndexError_, match="mailroom index"):
        index.table()
