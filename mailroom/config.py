"""Runtime settings: paths, model ids, limits, and API key loading.

Model ids are pinned here. The defaults are the ids this build was written
against; `mailroom models` confirms them against the live endpoint and
docs/MODELS.md records the result.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parent.parent

# Chat model used by eval config A and by the default triage path.
CHAT_MODEL: Final = os.environ.get("MAILROOM_CHAT_MODEL", "command-a-plus-05-2026")
# Smallest/cheapest Command model, used by eval config C as the cost baseline.
CHEAP_MODEL: Final = os.environ.get("MAILROOM_CHEAP_MODEL", "command-r7b-12-2024")
EMBED_MODEL: Final = os.environ.get("MAILROOM_EMBED_MODEL", "embed-v4.0")
RERANK_MODEL: Final = os.environ.get("MAILROOM_RERANK_MODEL", "rerank-v4.0-pro")

# Embed-v4.0 default output width, in floats.
EMBED_DIMENSION: Final = 1024

# USD per 1M tokens, as (input, output).
#
# NOT CONFIGURED. This table is deliberately empty: cost per letter is reported
# as "n/a" until real rates are entered here. Fill it from
# https://cohere.com/pricing, then re-run `mailroom eval`. Never estimate rates
# from memory.
PRICES_PER_MILLION: Final[dict[str, tuple[float, float]]] = {}

# Documented env var name for the API key. Never commit a value.
API_KEY_ENV: Final = "COHERE_API_KEY"
PASS_ENTRY: Final = "apis/cohere"


def load_api_key() -> str | None:
    """Return the Cohere key from the environment, else from `pass`.

    Returns None when neither source has a key. Callers must treat None as
    "cannot run a live call", never as an empty key.
    """
    env_key = os.environ.get(API_KEY_ENV, "").strip()
    if env_key:
        return env_key
    return _key_from_pass(PASS_ENTRY)


def _key_from_pass(entry: str) -> str | None:
    try:
        proc = subprocess.run(
            ["pass", "show", entry],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        if line.strip():
            return line.strip()
    return None


@dataclass(frozen=True)
class EvalConfig:
    """One row of the eval comparison: which models to use."""

    name: str
    chat_model: str
    embed_model: str
    rerank_model: str | None

    def describe(self) -> str:
        rerank = self.rerank_model or "none (vector only)"
        return f"{self.name}: chat={self.chat_model} embed={self.embed_model} rerank={rerank}"


EVAL_CONFIGS: Final[dict[str, EvalConfig]] = {
    "A": EvalConfig("A", CHAT_MODEL, EMBED_MODEL, RERANK_MODEL),
    "B": EvalConfig("B", CHAT_MODEL, EMBED_MODEL, None),
    "C": EvalConfig("C", CHEAP_MODEL, EMBED_MODEL, RERANK_MODEL),
}


@dataclass(frozen=True)
class Settings:
    """All filesystem and runtime settings for one process."""

    root: Path
    state_dir: Path
    index_dir: Path
    queue_path: Path
    runs_dir: Path
    playbook_dir: Path
    registry_path: Path
    golden_dir: Path
    results_dir: Path
    chat_model: str = CHAT_MODEL
    cheap_model: str = CHEAP_MODEL
    embed_model: str = EMBED_MODEL
    rerank_model: str | None = RERANK_MODEL
    backend: str = "cohere"
    max_steps: int = 8
    max_retries: int = 3
    timeout_seconds: float = 60.0
    requests_per_minute: int = 15
    retrieval_top_k: int = 20
    final_top_k: int = 3
    retry_backoff_seconds: float = 0.5

    @classmethod
    def build(
        cls,
        root: Path | None = None,
        backend: str | None = None,
        **overrides: object,
    ) -> Settings:
        base = Path(root) if root else REPO_ROOT
        state = base / ".mailroom"
        resolved_backend = backend or os.environ.get("MAILROOM_BACKEND", "cohere")
        values: dict[str, object] = {
            "root": base,
            "state_dir": state,
            "index_dir": state / "index",
            "queue_path": state / "queue.jsonl",
            "runs_dir": state / "runs",
            "playbook_dir": base / "data" / "playbook",
            "registry_path": base / "data" / "registry.json",
            "golden_dir": base / "evals" / "golden",
            "results_dir": base / "evals" / "results",
            "backend": resolved_backend,
        }
        values.update(overrides)
        return cls(**values)  # type: ignore[arg-type]

    def ensure_state_dirs(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    @property
    def is_fake(self) -> bool:
        return self.backend == "fake"


def price_for(model: str) -> tuple[float, float] | None:
    """Return (input, output) USD per 1M tokens, or None when not configured."""
    return PRICES_PER_MILLION.get(model)


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Estimated USD for one call, or None when the model has no configured price."""
    price = price_for(model)
    if price is None:
        return None
    in_rate, out_rate = price
    return (input_tokens / 1_000_000) * in_rate + (output_tokens / 1_000_000) * out_rate


__all__ = [
    "API_KEY_ENV",
    "CHEAP_MODEL",
    "CHAT_MODEL",
    "EMBED_DIMENSION",
    "EMBED_MODEL",
    "EVAL_CONFIGS",
    "PASS_ENTRY",
    "RERANK_MODEL",
    "REPO_ROOT",
    "EvalConfig",
    "Settings",
    "estimate_cost_usd",
    "load_api_key",
    "price_for",
]
