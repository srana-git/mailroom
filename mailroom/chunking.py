"""Read the playbook folder and cut it into indexable chunks.

A procedure is a markdown file with a small front matter block:

    ---
    id: PB-TAX-001
    title: Final notice from the tax agency - pay or contest
    owner: Owner (Shaw Rana)
    deadline_rule: Act within 10 business days of the letter date.
    language: en
    tags: [tax, final_notice, payment]
    ---

Short procedures stay whole. Long ones are split on blank lines and packed
greedily, and every chunk keeps its procedure id and title in its text so the
model can cite the id it retrieved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

MAX_CHUNK_CHARS = 900


@dataclass(frozen=True)
class PlaybookChunk:
    chunk_id: str
    playbook_id: str
    title: str
    owner: str
    deadline_rule: str
    tags: tuple[str, ...]
    text: str
    source_path: str


@dataclass
class _Procedure:
    meta: dict[str, str]
    body: str
    source_path: str
    tags: list[str] = field(default_factory=list)


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Split a leading `---` block into a flat string map and return the body.

    Deliberately tiny: only `key: value` lines, with optional surrounding quotes
    and bracket lists. It raises ValueError on a file with no front matter, so a
    malformed procedure fails loudly at index time instead of silently.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("missing front matter: file must start with '---'")
    # The closing marker is looked up before the keys are parsed: a file that
    # never closes its block has no body to speak of, so "not closed" is the
    # honest error, not "a line without a colon".
    end = next(
        (index for index in range(1, len(lines)) if lines[index].strip() == "---"),
        None,
    )
    if end is None:
        raise ValueError("front matter block is not closed")
    meta: dict[str, str] = {}
    for index in range(1, end):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"front matter line without ':' -> {line!r}")
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip().strip("'\"")
    body = "\n".join(lines[end + 1 :]).strip()
    return meta, body


def parse_tags(value: str) -> list[str]:
    cleaned = value.strip().strip("[]")
    return [part.strip().strip("'\"") for part in cleaned.split(",") if part.strip()]


def _split_body(procedure: _Procedure) -> list[str]:
    """Split a procedure body into chunks of at most MAX_CHUNK_CHARS."""
    header = (
        f"{procedure.meta['id']} - {procedure.meta.get('title', '')}".strip()
        + f"\nDeadline rule: {procedure.meta['deadline_rule']}"
        + f"\nOwner: {procedure.meta['owner']}"
    )
    paragraphs = [block.strip() for block in procedure.body.split("\n\n") if block.strip()]
    chunks: list[str] = []
    current = header
    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}"
        if len(candidate) > MAX_CHUNK_CHARS and current != header:
            chunks.append(current)
            current = f"{header}\n\n{paragraph}"
        else:
            current = candidate
    chunks.append(current)
    return chunks


def load_playbook_chunks(playbook_dir: Path) -> list[PlaybookChunk]:
    """Load every `.md` procedure, in filename order, as ready-to-embed chunks."""
    directory = Path(playbook_dir)
    chunks: list[PlaybookChunk] = []
    for path in sorted(directory.glob("*.md")):
        meta, body = parse_front_matter(path.read_text(encoding="utf-8"))
        for required in ("id", "title", "owner", "deadline_rule"):
            if not meta.get(required):
                raise ValueError(f"{path.name}: front matter needs '{required}'")
        procedure = _Procedure(
            meta=meta,
            body=body,
            source_path=path.name,
            tags=parse_tags(meta.get("tags", "")),
        )
        for index, text in enumerate(_split_body(procedure)):
            chunks.append(
                PlaybookChunk(
                    chunk_id=f"{meta['id']}#{index}",
                    playbook_id=meta["id"],
                    title=meta["title"],
                    owner=meta["owner"],
                    deadline_rule=meta["deadline_rule"],
                    tags=tuple(procedure.tags),
                    text=text,
                    source_path=path.name,
                )
            )
    return chunks


__all__ = [
    "MAX_CHUNK_CHARS",
    "PlaybookChunk",
    "load_playbook_chunks",
    "parse_front_matter",
    "parse_tags",
]
