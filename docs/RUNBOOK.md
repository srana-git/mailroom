# Runbook

This runbook is for the team that operates `mailroom`. It covers the daily loop,
how to teach the tool a new procedure, how to add a test case when a letter is
mis-handled, how to read a run, and how to rotate the API key.

Everything here is a command you run yourself. No step needs Shaw Rana, and no
step needs the author of the code.

## What the tool does

`mailroom` reads an official letter in French or English, decides what it is,
extracts the facts, finds the internal procedure that fits, and queues the next
move for a human to approve. It never pays, sends or files anything on its own.

## Where the state lives

| Path | What it holds |
| --- | --- |
| `.mailroom/index/` | The LanceDB vector index over the playbook, plus `meta.json` |
| `.mailroom/queue.jsonl` | Every proposed action and its decision. Append-only |
| `.mailroom/runs/<run_id>.jsonl` | The audit trail of one triage run |

All three live under the state directory. The default is `<repo>/.mailroom`. Pass
`--state-dir <path>` on any command to use another one, which is how the test
suite keeps its runs out of the real queue.

Nothing in `.mailroom/` is committed. It is in `.gitignore` and it is safe to
delete, at the cost of the history it holds.

## The daily loop

1. Put the letter in a file. A plain `.txt` works, so does a golden `.json`.
2. Run the triage:

   ```bash
   uv run mailroom triage /path/to/letter.txt --text-only
   ```

3. Read the status line.

   | Status | What it means | What to do |
   | --- | --- | --- |
   | `action_pending` | An action waits in the queue | Review it, then approve or reject |
   | `needs_human` | The agent will not settle this | Read the reason line and act by hand |
   | `filed` | Nothing to do | Nothing. The letter is recorded |
   | `error` | The run could not finish | Check the key and the network, then run again |

4. List what is waiting.

   ```bash
   uv run mailroom queue
   ```

5. Decide each item.

   ```bash
   uv run mailroom approve q_1a2b3c4d5e --note "checked against the notice"
   uv run mailroom reject q_1a2b3c4d5e --note "duplicate of last month"
   ```

   The effect is simulated and recorded. Approving does not move money. It marks
   the item so the team can see it was reviewed. Wire the real effect later, and
   only after your own change review.

An item marked `BLOCKED` was refused by the payee guard: the payment or send went
to an account or address that is not in `data/registry.json`. A blocked item
stays in the queue so you can see what the letter asked for. Never approve one
without a written decision by hand; the guard exists because a letter can ask for
a payment to a new account.

## Add a procedure

The playbook is `data/playbook/`. One file is one procedure.

1. Copy an existing file that is close to what you need, for example
   `data/playbook/PB-TAX-002.md`.
2. Give the new file a name that starts with its id: `PB-TAX-008.md`.
3. Fill the front matter. All four keys are required:

   ```markdown
   ---
   id: PB-TAX-008
   title: Interest charge on a late payment
   owner: Bookkeeping
   deadline_rule: Contest within 30 days of the letter date.
   language: en
   tags: [tax, interest]
   ---
   ```

   Write `deadline_rule` as one line. The loader rejects a procedure that is
   missing a key, and the test suite rejects a rule that wraps to two lines.

4. Write the body in three short sections: `## When this applies`, `## Steps`,
   `## If you cannot settle it`. Number the steps.
5. State the deadline in the body as well as in the front matter. The model reads
   the body; the front matter is metadata and is what the index carries.
6. Re-index (next section). The new procedure is invisible until you do.
7. Add a golden case for it only if the letter type is new to the set. See below.

Keep the file under about 900 characters per section. Longer procedures are cut
into chunks on blank lines, and a chunk split in the middle of a step reads
badly.

## Re-index

Run this after any playbook change, and after any change to the embedding model.

```bash
uv run mailroom index --force
```

Without `--force` the command reports the existing index and stops. The output
names the number of chunks, the number of procedures, the embedding model and the
vector width. Check the procedure count went up by the number of files you added.

`meta.json` records which embedding model built the index. Retrieval refuses to
read an index built by a different model, so a change to `EMBED_MODEL` in
`mailroom/config.py` needs `uv run mailroom index --force` before the next
triage. The failure message says so.

## Add a golden case when a letter is mis-handled

A golden case is a synthetic letter with the answer written down. The eval suite
scores the agent against it, and the same file is a regression test. Add one
every time a real letter is triaged wrongly.

1. Pick the family. The families are the folders in `evals/golden/`: `routine`,
   `payment`, `final`, `registry`, `info`, `payroll`, `phishing`, `injection`,
   `ocr`, `ambiguous`.
2. Copy a case from that family and rename it to `<family>-<next number>.json`,
   for example `evals/golden/payment-09.json`.
3. Set `id` to the file name without `.json`. A test enforces that.
4. Write `letter_text`. Use invented companies only: the `Exemple ...` family,
   or a numbered company. Use the reserved `.example` domain for any address.
   An `is_suspicious` label needs a `suspicion_reason`; a clean letter needs
   `suspicion_reason: null`.
5. Fill every gold field: `issuer`, `letter_type`, `language`, `reference_number`,
   `amount_due`, `currency`, `due_date` (ISO, and in 2026), `required_action`,
   `urgency` (1-5), `is_suspicious`, `suspicion_reason`.
6. Set `gold_playbooks` to the procedure ids that should be retrieved. Every id
   must exist in `data/playbook/`; a test enforces that.
7. Set `gold_action_kind` and `gold_needs_human`. An `injection` case also needs
   `gold_injection_target` with the payee and account the letter asked for.
8. Update the set size in the test. `tests/test_golden_set.py` pins the total and
   the per-family counts:

   ```python
   EXPECTED_FAMILIES = {"payment": 9, ...}   # raise the family you added to
   ```

   and `test_the_set_holds_sixty_letters` compares against the same total. Raise
   both numbers. The test suite fails loudly until you do, which is the point:
   the set size is a decision, not an accident.
9. Run the checks:

   ```bash
   uv run pytest -q tests/test_golden_set.py
   ```

10. Run the case end to end. This proves the pipeline reaches the label you
    wrote, not only that the file is well formed:

    ```bash
    uv run mailroom triage evals/golden/payment-09.json
    ```

11. Re-run the eval when live numbers matter:

    ```bash
    uv run mailroom eval --config A
    ```

    Results land in `evals/results/` and `docs/EVALS.md` is rewritten from them.
    Commit both so a published number can be traced to the run that produced it.

## Read a trace

Every run writes `.mailroom/runs/<run_id>.jsonl`. The triage prints the run id on
the first line.

```bash
uv run mailroom trace 1f0c9a2b3d4e
```

The header folds the run into counts: model calls, retries, tool calls, tokens
in and out, wall-clock milliseconds, and the final status and reason. Under it
comes every event in order.

| Event | Read this |
| --- | --- |
| `run_start` | `source` names the letter, `input_hash` identifies its text |
| `note` | The plan, and any wording in the letter that was aimed at the agent |
| `model_call` | `model`, `attempt`, `status`, `latency_ms`, `tokens_in`, `tokens_out` |
| `tool_call` | `tool`, `args`, `latency_ms`, and `guard` for a proposed action |
| `run_end` | The terminal `status` and the plain-language `reason` |

Three questions a trace answers:

- Did the model fail, or did it read the letter badly? Look for
  `model_call status=error` and at the `attempt` field.
- Did the guard block something? Look for `tool_call` lines with
  `guard=block`, then read the `guard.reasons` on the `run_end` line.
- What did it retrieve, and did it use it? The `search_playbook` line carries the
  procedure ids that came back.

A run that ends `needs_human` is not a failure. Read the reason: a blocked
payment, a suspected forgery, a letter that does not settle what must be done,
and a reading that failed validation twice all land there on purpose.

## Rotate the API key

The key is read from the environment variable `COHERE_API_KEY` first, then from
`pass show apis/cohere`. Never commit a key, and never paste one into an issue.

1. Create the replacement key in the Cohere dashboard.
2. Store it in one place, and remove it from the other, so the source of truth is
   never ambiguous.

   ```bash
   pass insert -m apis/cohere        # or: pass edit apis/cohere
   ```

   If you use the environment instead, put the export in your shell profile, not
   in the repository. `.env` is in `.gitignore`; a committed `.env` is a leak.
3. Confirm the key works and that the pinned model ids are still what the account
   offers:

   ```bash
   uv run mailroom models
   uv run mailroom models --write-docs
   ```

   The second command rewrites `docs/MODELS.md` with the live list.
4. Revoke the old key in the Cohere dashboard.
5. Run one letter to prove the path end to end:

   ```bash
   uv run mailroom triage evals/golden/routine-01.json --text-only
   ```

6. If the new account does not serve a pinned model, change the id in
   `mailroom/config.py` and re-index when the embedding model is the one that
   moved.

Rotating a key does not touch the index. Rotating the embedding model does.

## Before you push

```bash
uv run ruff check .
uv run pytest -q
```

CI runs the same two commands on every push and pull request. The test suite
needs no key and no network: it runs against a fake backend and recorded
responses. A test that needs the live API is marked `live` and is skipped by
default:

```bash
uv run pytest -q -m live
```

## When something looks wrong

- `no index at ... Run: uv run mailroom index` - the index is missing or the
  state directory moved. Re-index with `--force`.
- `index was built with embedding model ...` - the pin changed under an existing
  index. Re-index with `--force`.
- `No Cohere API key` - neither the variable nor `pass` had a key. Fix the key,
  or run with `--backend fake` for an offline demo that makes no call.
- The queue holds an item you expected to be gone - read it with
  `uv run mailroom queue --all`. A decided item is kept, not deleted.
- The triage found nothing for a letter you know the answer to - search the
  playbook by hand, then improve the procedure text and re-index. Retrieval
  scores words, so the fix is usually in the procedure, not in the model.
