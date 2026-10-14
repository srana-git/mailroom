# 2026-10-14-A.json, as first written

This is the v2 config A dev run exactly as the harness wrote it. One letter (`ocr-03`)
ended when the API returned HTTP 422 "invalid tool generation" for the model's tool call.
The harness of that day counted every final model error as a backend failure, so it
marked the run "not a valid measurement".

The rule changed the same day: a 400 or 422 is the agent's own output, so it is scored as
an agent miss and does not void the run. `../2026-10-14-A-rescored.json` is this run's
stored predictions re-scored under that rule, with the failure classes recounted from the
kept audit trails. No model call was made, and no score changed: only the validity count.
