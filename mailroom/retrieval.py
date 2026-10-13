"""The retrieval path: Embed the query, take the top 20 by vector distance,
then let Rerank cut that to the top 3.

Config B runs the same path with `rerank=False`, which is what makes B a fair
control for "does rerank earn its latency".
"""

from __future__ import annotations

from typing import Any

from .config import Settings
from .indexer import PlaybookIndex
from .models import RetrievalHit

# LanceDB returns `_distance` on its own; it must not appear in the projection.
_COLUMNS = ["chunk_id", "playbook_id", "title", "owner", "deadline_rule", "text"]


def _vector_score(distance: float) -> float:
    """Turn a LanceDB L2 distance into a score where bigger is better."""
    return 1.0 / (1.0 + max(0.0, float(distance)))


class Retriever:
    """Query the playbook index for the procedures that fit a letter."""

    def __init__(
        self,
        settings: Settings,
        backend: Any,
        index: PlaybookIndex | None = None,
        *,
        use_rerank: bool = True,
    ) -> None:
        self.settings = settings
        self.backend = backend
        self.index = index or PlaybookIndex(settings, backend)
        self.use_rerank = use_rerank and bool(settings.rerank_model)

    def search(self, query: str, *, top_k: int | None = None) -> list[RetrievalHit]:
        """Return the best procedures for this query, best first."""
        final_k = top_k or self.settings.final_top_k
        candidates = self._vector_search(query)
        if not candidates:
            return []
        if self.use_rerank:
            return self._rerank(query, candidates, final_k)
        return candidates[:final_k]

    def _vector_search(self, query: str) -> list[RetrievalHit]:
        table = self.index.table()
        query_vector = self.backend.embed(
            model=self.settings.embed_model,
            texts=[query],
            input_type="search_query",
        )[0]
        rows = (
            table.search(query_vector)
            .limit(self.settings.retrieval_top_k)
            .select(_COLUMNS)
            .to_list()
        )
        return [
            RetrievalHit(
                playbook_id=row["playbook_id"],
                title=row["title"],
                chunk_id=row["chunk_id"],
                text=row["text"],
                owner=row.get("owner", ""),
                deadline_rule=row.get("deadline_rule", ""),
                score=_vector_score(row.get("_distance", 0.0)),
                stage="vector",
            )
            for row in rows
        ]

    def _rerank(
        self, query: str, candidates: list[RetrievalHit], final_k: int
    ) -> list[RetrievalHit]:
        results = self.backend.rerank(
            model=self.settings.rerank_model or "",
            query=query,
            documents=[hit.text for hit in candidates],
            top_n=final_k,
        )
        hits: list[RetrievalHit] = []
        for result in results:
            if result.index < 0 or result.index >= len(candidates):
                continue
            base = candidates[result.index]
            hits.append(
                base.model_copy(update={"score": float(result.score), "stage": "rerank"})
            )
        return hits


def dedupe_by_playbook(hits: list[RetrievalHit], limit: int | None = None) -> list[RetrievalHit]:
    """Keep the best chunk per procedure, preserving order.

    Retrieval metrics are scored on procedure ids, so two chunks of the same
    procedure must not take two of the three slots.
    """
    seen: set[str] = set()
    unique: list[RetrievalHit] = []
    for hit in hits:
        if hit.playbook_id in seen:
            continue
        seen.add(hit.playbook_id)
        unique.append(hit)
    return unique[:limit] if limit else unique


__all__ = ["Retriever", "dedupe_by_playbook"]
