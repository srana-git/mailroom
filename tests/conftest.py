"""Shared fixtures.

Everything runs offline against the fake backend and the real playbook corpus.
Nothing here reaches the network, so `uv run pytest` is safe on a plane and in CI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mailroom.agent import Triager
from mailroom.config import REPO_ROOT, Settings
from mailroom.fake import FakeBackend
from mailroom.indexer import PlaybookIndex
from mailroom.queue import ActionQueue
from mailroom.registry import Registry, load_registry
from mailroom.retrieval import Retriever

INJECTION_CASE = REPO_ROOT / "evals" / "golden" / "injection-01.json"
GOLDEN_DIR = REPO_ROOT / "evals" / "golden"


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    state = tmp_path / "state"
    return Settings.build(
        root=REPO_ROOT,
        backend="fake",
        state_dir=state,
        index_dir=state / "index",
        queue_path=state / "queue.jsonl",
        runs_dir=state / "runs",
    )


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def index(settings: Settings, backend: FakeBackend) -> PlaybookIndex:
    playbook_index = PlaybookIndex(settings, backend)
    playbook_index.build(force=True, verbose=False)
    return playbook_index


@pytest.fixture
def registry(repo_root: Path) -> Registry:
    return load_registry(repo_root / "data" / "registry.json")


@pytest.fixture
def action_queue(settings: Settings) -> ActionQueue:
    return ActionQueue(settings.queue_path)


@pytest.fixture
def retriever(settings: Settings, backend: FakeBackend, index: PlaybookIndex) -> Retriever:
    return Retriever(settings, backend, index=index)


@pytest.fixture
def triager(
    settings: Settings,
    backend: FakeBackend,
    retriever: Retriever,
    registry: Registry,
    action_queue: ActionQueue,
) -> Triager:
    return Triager(
        backend=backend,
        retriever=retriever,
        registry=registry,
        queue=action_queue,
        settings=settings,
    )
