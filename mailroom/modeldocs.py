"""Render docs/MODELS.md.

The pinned ids and the live list live in one generated file so the two cannot
disagree. `mailroom models --write-docs` regenerates it: without a key it writes
the pinned table and says plainly that the live endpoint was not reached.
"""

from __future__ import annotations

from typing import Any

from .config import CHAT_MODEL, CHEAP_MODEL, EMBED_DIMENSION, EMBED_MODEL, RERANK_MODEL

PREAMBLE = """\
# Models

`mailroom/config.py` pins one model per role. The pinned ids are the defaults
this build was written against. `mailroom models --write-docs` replaces the live
list below with what the key can actually see, and the pin is only trustworthy
once that list confirms it.

| Role | Pinned id | Used by |
| --- | --- | --- |
| Chat, best | `{chat}` | triage, eval config A and B |
| Chat, cheap | `{cheap}` | eval config C |
| Embed | `{embed}` | the playbook index, and every query |
| Rerank | `{rerank}` | retrieval, eval config A and C |
| Embed width | {dimension} floats | the LanceDB table schema |

Override a pin without editing code by setting `MAILROOM_CHAT_MODEL`,
`MAILROOM_CHEAP_MODEL`, `MAILROOM_EMBED_MODEL` or `MAILROOM_RERANK_MODEL`.

## How to confirm the pins

```bash
uv run mailroom models               # print what the key can use
uv run mailroom models --write-docs  # and rewrite this file
```

If a pin is wrong, the first sign is a `404` or `400` from the chat or embed
call. Change the pin in `mailroom/config.py`, run `uv run mailroom index --force`,
and run the eval again.

An index is tied to the embed model that built it. `mailroom index` writes
`.mailroom/index/meta.json`; retrieval refuses to read an index built by a
different model rather than returning quietly wrong neighbours.
"""

NOT_CONFIRMED_NOTE = """\
## Live model list

Not recorded. `mailroom models --write-docs` has not been run against the live
models endpoint, so the pinned ids above are the build defaults rather than a
confirmed list. Run `uv run mailroom models --write-docs` once a key is in place.
"""


def render_models_doc(models: list[dict[str, Any]] | None) -> str:
    parts = [
        PREAMBLE.format(
            chat=CHAT_MODEL,
            cheap=CHEAP_MODEL,
            embed=EMBED_MODEL,
            rerank=RERANK_MODEL,
            dimension=EMBED_DIMENSION,
        ).rstrip(),
        "",
    ]
    if not models:
        parts.append(NOT_CONFIRMED_NOTE.rstrip())
        return "\n".join(parts) + "\n"

    parts.append("## Live model list")
    parts.append("")
    parts.append(f"The key returned {len(models)} models.")
    parts.append("")
    parts.append("| Model | Endpoints | Context |")
    parts.append("| --- | --- | --- |")
    for model in models:
        endpoints = ", ".join(model.get("endpoints") or []) or "-"
        context = model.get("context_length")
        parts.append(f"| `{model.get('name', '')}` | {endpoints} | {context or '-'} |")
    return "\n".join(parts) + "\n"


__all__ = ["render_models_doc"]
