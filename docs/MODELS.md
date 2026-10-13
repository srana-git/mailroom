# Models

`mailroom/config.py` pins one model per role. The pinned ids are the defaults
this build was written against. `mailroom models --write-docs` replaces the live
list below with what the key can actually see, and the pin is only trustworthy
once that list confirms it.

| Role | Pinned id | Used by |
| --- | --- | --- |
| Chat, best | `command-a-plus-05-2026` | triage, eval config A and B |
| Chat, cheap | `command-r7b-12-2024` | eval config C |
| Embed | `embed-v4.0` | the playbook index, and every query |
| Rerank | `rerank-v4.0-pro` | retrieval, eval config A and C |
| Embed width | 1024 floats | the LanceDB table schema |

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

## Live model list

Not recorded. `mailroom models --write-docs` has not been run against the live
models endpoint, so the pinned ids above are the build defaults rather than a
confirmed list. Run `uv run mailroom models --write-docs` once a key is in place.
