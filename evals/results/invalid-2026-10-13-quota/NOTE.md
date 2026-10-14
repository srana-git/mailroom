# Not valid measurements: kept as a record

These three dev-set runs (configs A, B and C, 2026-10-13) ran out of the trial key's
1,000 calls per month. Every model call after that returned HTTP 429.

| Run | Letters | Letters with a model call that failed for good |
| --- | --- | --- |
| A, golden | 60 | 45 (the first 15 letters ran clean) |
| B, golden | 60 | 60 |
| C, golden | 60 | 60 |

The agent failed closed on each of those letters, so no write happened. The harness
still scored them, and the scores measure the quota, not the agent. The harness now
counts these letters (`backend_failures`), flags the run as not valid, and stops after
3 such letters in a row.

The held-out run of the same day (`../2026-10-13-A-holdout.json`) finished before the
quota ran out and is valid: 19 of 20 letters ran clean, and 1 failed on a real request
bug that is now fixed.
