# Model backend health checks

Every production deploy (`k8s/deploy.yaml`) runs an end-to-end health check
of all **enabled** model backends
([`fighthealthinsurance/ml/model_health_check.py`](../fighthealthinsurance/ml/model_health_check.py)).
The staging and dev manifests (`k8s/deploy_staging.yaml`, `k8s/deploy_dev.yaml`)
have no job that runs it. Each backend gets one tiny "Reply with exactly: OK"
inference, one attempt with no retries, all backends at once. The probe uses
the same code path as the web pods, run inside the `web-actor-launch` Job. The
Job also mounts `fight-health-insurance-primary-secret`, which the web
Deployment does not, so their credential environments are not identical: a
key kept only in that secret would pass the probe and still be missing on
the web pods.
A backend already in rate-limit back-off is reported `FAIL_RATE_LIMITED`
without being called.

Results are categorized, persisted for the staff status page, and logged as a
greppable summary block.

| Category | Meaning | Counts as a failure |
| --- | --- | --- |
| `PASS` | Answered. | No |
| `PASS_UNREGISTERED` | Answered, but the router did not register it. | No |
| `NOT_CONFIGURED` | No configuration; listed, never called. | No |
| `DISABLED` | Excluded by `ENABLED_REMOTE_MODELS` ([ml-backends.md](ml-backends.md)); listed, never called. | No |
| `FAIL_MISSING_CREDENTIALS`, `FAIL_CLIENT_INIT`, `FAIL_AUTH`, `FAIL_MODEL_NOT_FOUND`, `FAIL_RATE_LIMITED`, `FAIL_TIMEOUT`, `FAIL_NETWORK`, `FAIL_MALFORMED_RESPONSE`, `FAIL_OTHER` | What went wrong. | Yes |

Only the `FAIL_*` categories trigger the alert email or strict mode.

## How the deployment hook is invoked

`scripts/start-server.sh` runs `python manage.py check_model_backends --deploy-hook`
in the `web-actor-launch` Job (the `POLLING_ACTORS=1` container in
`k8s/deploy.yaml`), right after `launch_polling_actors`. If that launch fails,
the script sleeps 480 seconds before the check.

The migrations Job (`web-migrations`) and this Job are applied together by
`scripts/build.sh`, and nothing orders them. The check tolerates that: if the
schema is not migrated yet, the leader claim and the result rows fail soft, so
the check skips or runs without persisting rather than crashing.

## Leader election and duplicate-run prevention

`--deploy-hook` first claims a row in the shared database
(`ModelHealthAlertState.try_claim`: a conditional UPDATE, or a unique-key
insert the first time) keyed on the deployment identifier:
`FHI_DEPLOYMENT_ID`, then `FHI_RELEASE` (baked into the image from the build
`RELEASE` arg), then `FHI_VERSION`, with an hourly timestamp fallback.

- Exactly one process per deployment wins. Every other pod, worker or Job
  retry sees the claim taken and skips, so the check runs (and the alert email
  is sent) at most once per deployment. If the winner crashes mid-check,
  retries still skip.
- Claims expire after 6 hours, so re-deploying the same version later still
  re-checks. Re-deploying the same version within 6 hours does not; run the
  command by hand then.
- The Dockerfile's `RELEASE` default is `unknown`, so `FHI_RELEASE` is never
  empty inside a built image. An image built without `--build-arg RELEASE`
  shares one claim key with every other such build.

## Running it manually

From any pod or local shell with the environment configured:

```bash
# All enabled backends; exits 1 if any check fails
python manage.py check_model_backends

# One model (friendly registry name or wire/internal name), custom timeout.
# --model can be repeated.
python manage.py check_model_backends --model anthropic/claude-sonnet-4-6 --timeout 15

# Skip writing rows to the results table
python manage.py check_model_backends --no-persist
```

- The default timeout is 30 seconds, or `FHI_MODEL_HEALTH_TIMEOUT` when set.
- Manual runs write result rows unless `--no-persist` is given, and never send
  the email.
- Exit codes: a manual run exits 1 on any failure (or when the check could not
  run). The deploy hook exits 0, or 2 in strict mode, which
  `start-server.sh` turns into 1.

## Alerting

When any enabled backend fails, the deploy hook sends **one** consolidated
email to `support42@fighthealthinsurance.com` listing every failing backend
(provider, model, internal key, failure category, sanitized error, registry
state) plus the deployment id, environment, timestamp, and where to look next.
Backends that work but are not registered are listed too, but only when at
least one real failure triggered the email.

`FHI_MODEL_HEALTH_ALERT_EMAIL` controls it:

| Value | Effect |
| --- | --- |
| unset (or anything other than `1`/`0`) | On in production; off when `settings.DEBUG` is true or `TESTING=True`. |
| `1` | On anywhere. |
| `0` | Off everywhere. |

Only the leader can send it, and only for `--deploy-hook` runs, so it has no
effect on a local `run_local.sh` session, which never runs the hook. Error text
is sanitized (API keys, Authorization/x-api-key headers, and secret-looking
environment values are redacted) before logging, storing, or emailing.

## Strict mode

By default a failing backend does NOT fail the deployment (healthy backends
keep serving; the failure is logged and emailed). `FHI_MODEL_HEALTH_STRICT=1`
makes the first attempt of the `web-actor-launch` Job fail. It does not block
or roll back a deploy today:

- The Job has `restartPolicy: OnFailure`. The retry finds the leader claim
  already taken, skips the check, and exits 0, so the Job completes.
- `scripts/build.sh` does not wait on this Job.
- The shell test in `start-server.sh` honors only the exact value `1`. The
  Python side also accepts `true` and `yes`, but then the command's exit code
  2 is reported as non-blocking.

## Where to inspect results

- **Deployment logs:** search for `MODEL_BACKEND_HEALTH_SUMMARY` in the
  `web-actor-launch` Job output: a header line (run, deployment, environment,
  checked and failed counts), then one line per backend with category,
  latency, and sanitized detail. The Job is deleted 10 seconds after it
  finishes (`ttlSecondsAfterFinished: 10`), so read this from your log
  aggregator or the staff page, not `kubectl logs`.
- **Staff dashboard:** `/timbit/help/model_backends` shows, per configured
  model, enabled/disabled state, provider, registry name, internal key,
  selection-UI/reporting registration, the latest check result and timestamp,
  and the last stored generation. It makes no model calls.
  `/timbit/help/model_usage` shows which models users actually pick.
- **Database:** `ModelBackendHealthCheckResult` keeps one row per backend per
  run (including `NOT_CONFIGURED` and `DISABLED`). Skipped runs and
  `--no-persist` runs write nothing.

## Backfilling historical metadata

`python manage.py backfill_model_metadata` normalizes legacy `model_name`
values on `ProposedAppeal`/`ChooserCandidate` rows to the canonical registry
names the dashboard aggregates: old object reprs (only when the backend class
has a single model), `ClassName(model)` descriptors, and unambiguous bare wire
ids. Dry-run by default; `--apply` to write. Ambiguous or unrecognized values
are reported and left untouched. NULL rows stay unattributed on the dashboard:
`(unattributed)` for picks made after model tracking began, and
`legacy-unattributed` for older rows.
