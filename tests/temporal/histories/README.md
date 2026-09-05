# Recorded workflow histories

Drop production workflow histories here as `<something>.json` and
`test_workflow_replay.py` will replay them against the current workflow code
on every CI run.

## Why

`IntakeJourneyWorkflow` runs for **30 days**. A workflow-code change deployed
inside that window is replayed against histories written by the old code. If
the change alters the sequence of commands the workflow issues, those
in-flight runs fail with a non-determinism error — not at deploy time, but
later and one at a time, on workflows a patient is waiting on.

Replaying a real history in CI turns that into a failed build instead.

## Capturing one

```sh
kubectl -n totallylegitco exec deploy/temporal-admintools -- \
  temporal workflow show \
    --address temporal-frontend:7233 --namespace default \
    --workflow-id <workflow-id> --output json \
  > tests/temporal/histories/<name>.json
```

Prefer a history that exercised the interesting paths: a nudge, a
reconciliation, a completion. One of each beats ten of the same shape.

## Before you check one in

These are production histories. `TEMPORAL_PAYLOAD_KEY` is unset in production
today, so payloads are **plaintext in the JSON** — and while the workflow
inputs are ids-only by design (`hashed_email`, `denial_uuid`,
`contact_opt_in`), read the file before committing it and confirm nothing else
rode along. If payload encryption is ever turned on, a captured history will
need the codec to replay.
