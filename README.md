# mailroom

An agent that reads official mail in French or English, works out what each letter wants,
finds the internal procedure that applies, and queues the next step for a person to approve.
Built on Cohere: Command A+ for the agent, Embed v4 and Rerank v4 for retrieval.

## Why

I run the back office for a group of small Québec companies. Tax agencies, the business
registry, payroll and cities post letters to online inboxes and nobody reads them on time.
A missed final notice costs real money. Every letter needs the same work: what is it, how
much, by when, what do we do, and is it even real. This repo is that workflow, rebuilt as a
clean, testable agent with invented companies and letters, so it can be shown.

## What it does

- Reads a letter and extracts the facts: issuer, letter type, language, reference number,
  amount, currency, due date, required action, urgency, and whether it looks fraudulent.
- Retrieves the matching procedure from a playbook of 38 internal procedures
  (vector search in LanceDB, then Rerank).
- Plans, calls tools, and proposes one action: pay, contest, file, escalate, or nothing.
- **Never executes anything.** Every proposed action goes to an approval queue.
- Writes an audit trail for every run: each model call, tool call, guard decision, latency and
  token count.

## Who decides what

| Decision | Made by | Why |
| --- | --- | --- |
| Does the letter contain instructions aimed at the agent? | Code (regex rules, FR/EN) | A prompt cannot be the only defence against a prompt. |
| Is a payment going to an account we know? | Code (registry lookup) | The model can be talked into anything; the payee list cannot. |
| Fraud signals (unknown account, gift cards, look-alike domain, threats) | Code, OR-ed with the model | Recall matters more than elegance here. |
| Body language (FR/EN) | Code, overriding the model | A French letterhead fooled the model on English letters. See v1. |
| What the letter is and what it asks for | Command A+ | This is the part that needs reading, not rules. |
| Which procedure applies | Embed + Rerank, plus forced security procedures when a guard fires | |
| Pay, file, contest, escalate | **A person**, from the queue | Anything that moves money or speaks for the company. |

```mermaid
flowchart LR
  L[Letter] --> G1[Guards: injection, fraud, language]
  G1 --> P[Command A+: plan]
  P --> X[extract_fields]
  P --> S[search_playbook]
  S --> E[Embed v4 → LanceDB top 20] --> R[Rerank v4 → top 3]
  P --> C[lookup_company]
  P --> A[propose_action]
  A --> G2[Payee guard]
  G2 --> Q[(Approval queue)]
  Q --> H[Person approves or rejects]
  G1 -. audit .-> T[(Run trace JSONL)]
  P -. audit .-> T
  G2 -. audit .-> T
```

## Results

All live runs, Cohere trial key, 2026-10-13. Every number is copied from a result file in
`evals/results/`; [`docs/EVALS.md`](docs/EVALS.md) defines each metric.

| Run | Letters | All fields exact | Core fields exact | Retrieval recall@3 | Phishing flagged | Injection resisted | Unapproved writes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| v1, A (Command A+, Embed, Rerank), as first scored | 60 dev | 23% | – | 0.62 | 3 of 5 | 6 of 6 | 0 |
| v1, A, re-scored under the v2 rules | 60 dev | 42% | 72% | 0.62 | 3 of 5 | 6 of 6 | 0 |
| v1, B (no rerank), re-scored | 60 dev | 40% | 70% | 0.61 | 3 of 5 | 6 of 6 | 0 |
| v1, C (Command R7B), re-scored | 60 dev | 17% | 38% | 0.55 | 1 of 5 | 6 of 6 | 0 |
| **v2, A, held-out letters never used for tuning** | **20 held-out** | **45%** | **60%** | **0.70** | **2 of 2** | **2 of 2** | **0** |

Read it this way:

- **Most of the jump from 23% to 42% is the metric getting fairer, not the agent getting
  better.** The re-scored v1 row shows that, which is why it is there.
- **The held-out row is the honest v2 number.** It is a different, smaller set, so it is not a
  like-for-like comparison with the dev rows. 20 letters is a small sample.
- **Safety held in every run:** 0 unapproved writes in 200 live runs, because the queue and the
  guards are code, not prompt.
- **Rerank did not earn its place on this set:** recall@3 0.62 with it, 0.61 without. The
  playbook is small (38 procedures); I would expect rerank to matter more on a larger one, but
  that is a guess until it is measured.
- **The small model costs real accuracy:** Command R7B lost about half the core-field accuracy
  and caught 1 of 5 phishing letters by itself. The code guards still blocked every injection.
- **Time per letter (held-out, v2):** model time p50 6.6 s; wall time p50 10.3 s. The gap is the
  client-side rate limiter for the trial key.

What the held-out run still gets wrong:

- **It sends more letters to a person than the gold expects** (needs_human precision 0.50).
  On 4 letters the agency is real but the account number on the letter is not in the
  registry, so the payee guard sent the payment to a person. That is the guard working as
  designed: a known name with a new account is how payment fraud looks. The held-out gold
  expected a plain payment. I left the gold as written, because changing a held-out label
  after seeing the result would defeat the held-out set.
- **On both ambiguous letters it proposed a payment instead of escalating.** A person still
  approves every payment, so nothing moves, but the agent's call was wrong.
- **One letter failed on a request bug:** after a tool turn, the agent sent a `tool_plan` the
  API rejected. Fixed: a tool turn now goes back as `tool_plan` plus `tool_calls` only.

The v2 rerun of all three configs on the dev set ran out of the trial key's 1,000 calls per
month part way through. Those runs are kept in
[`evals/results/invalid-2026-10-13-quota/`](evals/results/invalid-2026-10-13-quota/NOTE.md)
and are not reported as results. The harness now catches this: it counts letters where a model
call failed for good, marks the run as not valid, and stops after 3 in a row.

## What the first eval taught me

The first live run (v1) looked like this on the best config: 23% of letters with every field
exactly right, retrieval recall@3 of 0.62, phishing recall of 60%. Safety was already solid:
zero unapproved writes and 100% injection resistance, because those are enforced in code.

Before changing anything I sorted every one of the 93 field misses into one of four buckets
([`docs/ERROR-ANALYSIS-2026-10-13.md`](docs/ERROR-ANALYSIS-2026-10-13.md)):

- **46 real model errors.** The biggest surprise: 7 English letters labelled French because
  of a French letterhead and signature. Fixed with a deterministic body-language check.
- **20 ambiguous labels.** Two answers were both defensible. Fixed by writing the decision
  rules into the prompt and the tool schema (for example, a relative "within 30 days" is
  counted from the letter date, not guessed).
- **17 metric errors.** The scorer rejected "Revenu Québec – Direction principale des taxes"
  against "Revenu Québec". Fixed by matching on the organisation, with negative tests so it
  cannot become a fuzzy match.
- **10 wrong gold labels.** Each correction quotes the letter line that proves it:
  [`evals/GOLD-CHANGELOG.md`](evals/GOLD-CHANGELOG.md).

Two engineering bugs also showed up. The eval harness wrote its traces to a temp directory and
lost them, so latency could not be broken down; traces now persist, and model time is
reported apart from wall time. And the live API returns message content as a list of blocks,
which the offline fake never did.

To keep the v2 numbers honest:

- The v1 predictions are **re-scored under the v2 rules**, so the table separates "the metric
  got fairer" from "the system got better".
- A **20-letter held-out set** was written and committed before any v2 change
  (`6e3e9d4`) and was never used for tuning.

## Quickstart

```bash
uv sync
export COHERE_API_KEY=...            # a trial key works
uv run mailroom index                # embed the playbook into LanceDB
uv run mailroom triage evals/golden/injection-01.json --text-only
uv run mailroom trace <run_id>       # read every step of that run
uv run mailroom queue                # what is waiting for a person
uv run mailroom eval --config A      # score the golden set
```

Offline, with no key: `uv run pytest -q` (389 tests, a fake backend replays recorded model
behaviour) or `uv run mailroom --backend fake triage …`.

## Design notes

- **Plan-and-execute, not free ReAct.** The model writes a short plan first and it is
  recorded, but the loop decides the outcome from what the tools actually returned, not from
  what the model says it did. The plan is advisory. In one live run the model planned an
  `update_account` tool that does not exist; nothing happened, because only real tools run.
- **Fail closed.** Every tool argument is validated with pydantic. A bad argument goes back to
  the model once; a second failure ends the run as `needs_human`.
- **Rerank is measured, not assumed.** Config B (no rerank) runs beside A. On this playbook it
  made almost no difference, and the README says so.
- **The cheap model is in the table too** (config C), so the cost of quality is visible.

## Limits

- The letters are synthetic: 60 dev letters and 20 held-out letters. They test that the
  pipeline works and fails safely, not how it performs on a real mailbox.
- Cost per letter is not reported in dollars. Cohere's public page does not list per-token
  rates for every model used, and a guessed rate is worse than none. Tokens per letter are
  reported instead.
- Measured on a trial key with a client-side rate limiter; wall time includes that wait,
  model time does not.

## Repo map

| Path | What |
| --- | --- |
| `mailroom/agent.py` | The plan-and-execute loop |
| `mailroom/guards.py` | Deterministic guards (injection, fraud, language, payee) |
| `mailroom/retrieval.py`, `indexer.py` | Embed → LanceDB → Rerank |
| `mailroom/tools.py` | Tool schemas the model sees |
| `mailroom/queue.py`, `audit.py` | Approval queue, run traces |
| `mailroom/evaluation/` | Scoring, re-scoring, reports |
| `data/playbook/` | 38 procedures |
| `evals/golden/`, `evals/holdout/` | 60 dev letters, 20 held-out letters |
| `docs/RUNBOOK.md` | For the team that runs it: add a procedure, add a test case, read a trace, rotate keys |

All companies, people and letters are invented.
