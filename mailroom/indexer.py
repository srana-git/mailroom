"""Build and open the LanceDB vector index over the playbook.

One table, one row per chunk, one embedding per row. `mailroom index` is the
only writer. A `meta.json` beside the table records which embedding model and
which width produced it, so a stale index is detected instead of silently
returning nonsense.
"""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from typing import Any

import lancedb

from .chunking import load_playbook_chunks
from .config import Settings

TABLE_NAME = "playbook"
META_NAME = "meta.json"


class IndexError_(RuntimeError):
    """Raised when the index is missing or was built with a different model."""


class PlaybookIndex:
    """Thin wrapper over the LanceDB table."""

    def __init__(self, settings: Settings, backend: Any) -> None:
        self.settings = settings
        self.backend = backend
        self.uri = settings.index_dir
        self.meta_path = self.uri / META_NAME

    # -- meta ------------------------------------------------------------

    def read_meta(self) -> dict[str, Any] | None:
        if not self.meta_path.exists():
            return None
        try:
            return json.loads(self.meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def _check_fresh(self) -> dict[str, Any]:
        meta = self.read_meta()
        if meta is None:
            raise IndexError_(
                f"no index metadata at {self.meta_path}. Run: uv run mailroom index"
            )
        if meta.get("embed_model") != self.settings.embed_model:
            raise IndexError_(
                "index was built with embedding model "
                f"'{meta.get('embed_model')}', but settings now use "
                f"'{self.settings.embed_model}'. Run: uv run mailroom index --force"
            )
        return meta

    # -- build -----------------------------------------------------------

    def build(self, *, force: bool = False, verbose: bool = True) -> dict[str, Any]:
        """Embed every playbook chunk and write the table. Returns the metadata."""
        chunks = load_playbook_chunks(self.settings.playbook_dir)
        if not chunks:
            raise IndexError_(f"no playbook files under {self.settings.playbook_dir}")

        vectors = self.backend.embed(
            model=self.settings.embed_model,
            texts=[chunk.text for chunk in chunks],
            input_type="search_document",
        )
        if len(vectors) != len(chunks):
            raise IndexError_(
                f"embedding count {len(vectors)} does not match chunk count {len(chunks)}"
            )
        dimension = len(vectors[0]) if vectors else 0

        if verbose:
            print(
                f"embedding {len(chunks)} chunks with {self.settings.embed_model} "
                f"({dimension} dimensions)"
            )

        self.uri.mkdir(parents=True, exist_ok=True)
        rows = [
            {
                "chunk_id": chunk.chunk_id,
                "playbook_id": chunk.playbook_id,
                "title": chunk.title,
                "owner": chunk.owner,
                "deadline_rule": chunk.deadline_rule,
                "source_path": chunk.source_path,
                "text": chunk.text,
                "vector": vector,
            }
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]

        database = lancedb.connect(str(self.uri))
        if force or self._has_table(database):
            with contextlib.suppress(ValueError, OSError):
                database.drop_table(TABLE_NAME)
        database.create_table(TABLE_NAME, data=rows)

        procedures = len({chunk.playbook_id for chunk in chunks})
        meta = {
            "table": TABLE_NAME,
            "embed_model": self.settings.embed_model,
            "dimension": dimension,
            "chunks": len(chunks),
            "procedures": procedures,
            "backend": getattr(self.backend, "name", "unknown"),
            "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        self.meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        return meta

    @staticmethod
    def _has_table(database: Any) -> bool:
        try:
            listing = database.list_tables()
        except AttributeError:
            return TABLE_NAME in database.table_names()
        names = getattr(listing, "tables", listing)
        return TABLE_NAME in list(names)

    # -- read ------------------------------------------------------------

    def table(self) -> Any:
        if not self.uri.exists():
            raise IndexError_(
                f"no index at {self.uri}. Run: uv run mailroom index"
            )
        self._check_fresh()
        return lancedb.connect(str(self.uri)).open_table(TABLE_NAME)


__all__ = ["META_NAME", "TABLE_NAME", "IndexError_", "PlaybookIndex"]
