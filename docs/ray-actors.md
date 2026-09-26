# Ray actors

The application uses Ray actors for background loops such as email polling,
fax polling and chooser refill. They run continuously, as detached actors
(`lifetime="detached"`) in the Ray namespace `fhi`, on the deployed Ray
cluster. The local dev server (`scripts/run_local.sh`) does not start them.

## The polling actors

| Ray actor name | Code | What it does | Launched when |
| --- | --- | --- | --- |
| `email_polling_actor` | `fighthealthinsurance/email_polling_actor.py` | Sends follow-up, thank-you and scheduled emails | Always |
| `fax_polling_actor` | `fighthealthinsurance/fax_polling_actor.py` | Sends queued faxes | Only when `TEMPORAL_ENABLED` is false (the default); with Temporal on, `SendFaxWorkflow` replaces it |
| `chooser_refill_actor` | `fighthealthinsurance/chooser_refill_actor.py` | Keeps the chooser's task pool full | Always |
| `imr_refresh_actor` | `fighthealthinsurance/imr_refresh_actor.py` | Refreshes the IMR / external-review corpus | Always |
| `ucr_refresh_actor` | `fighthealthinsurance/ucr_refresh_actor.py` | Refreshes UCR rate data | Always |
| `pa_refresh_actor` | `fighthealthinsurance/pa_refresh_actor.py` | Refreshes carrier prior-auth requirement lists | Always |

The same launch also creates `speculative_appeals_actor`, which precomputes
candidate appeals. It has no polling loop and is not part of the health check
below. The launch list is in `fighthealthinsurance/polling_actor_setup.py`; the
health-check list is in `fighthealthinsurance/actor_health_status.py`.

How they are started and kept alive:

- The `web-actor-launch` Job in `k8s/deploy.yaml` launches them once per
  deploy (`POLLING_ACTORS=1`, see `scripts/start-server.sh`).
- The `fhi-actor-reconcile` CronJob (`k8s/actor-reconcile-cronjob.yaml`) runs
  `reconcile_polling_actors` every 5 minutes. It relaunches any actor that is
  missing, and replaces any whose health check answers False: one whose loop
  has stopped, or the chooser refill actor after three failed ticks in a row.

## Monitoring actor health

The REST endpoint `/ziggy/rest/actor_health_status` reports on the polling
actors. It needs no login, and responses carry
`Cache-Control: public, max-age=60`, so a status read through a cache can be
up to a minute stale right after a relaunch. Staff can see the same data at
`/timbit/help/status`.

```bash
# Local dev server (self-signed certificate, hence -k)
curl -k https://localhost:8000/ziggy/rest/actor_health_status

# Inside a production web pod (uvicorn listens on 8010 behind nginx)
curl http://localhost:8010/ziggy/rest/actor_health_status
```

Locally, with `RAY_ADDRESS` unset or set to `local`, every actor reports
`"alive": false` with `"error": "no ray cluster available"`. That is expected.

Response format (six actors when `TEMPORAL_ENABLED` is false; five, without
`fax_polling_actor`, when it is true):

```json
{
  "alive_actors": 6,
  "total_actors": 6,
  "details": [
    {"name": "email_polling_actor", "alive": true, "error": null},
    {"name": "fax_polling_actor", "alive": true, "error": null},
    {"name": "chooser_refill_actor", "alive": true, "error": null},
    {"name": "imr_refresh_actor", "alive": true, "error": null},
    {"name": "ucr_refresh_actor", "alive": true, "error": null},
    {"name": "pa_refresh_actor", "alive": true, "error": null}
  ]
}
```

Possible `error` values: `"no ray cluster available"`, `"actor not found"`,
`"health_check timeout"`, `"health_check returned False"`, and
`"health_check error: ..."`. If the view itself fails, it returns
`alive_actors: 0`, an empty `details` list and a `message` key.

## Relaunching actors

All of these must run where the Ray cluster is reachable (a production pod
with `RAY_ADDRESS` set, for example through `kubectl exec`).

```bash
# Missing, crashed or unhealthy actors: relaunch what is absent and replace
# what answers its health check with False; healthy actors are left alone
# (idempotent). Exits 0 when every actor is alive at the end, 1 when any is
# still missing.
# The fhi-actor-reconcile CronJob already runs this every 5 minutes.
python manage.py reconcile_polling_actors
python manage.py reconcile_polling_actors --dry-run   # report only

# Wedged in a way the health check misses (answering True, or not answering
# at all): kill every polling actor, healthy ones included, then recreate.
python manage.py launch_polling_actors --force

# The deploy-time launcher (what start-server.sh runs when POLLING_ACTORS is set).
python manage.py launch_polling_actors
```

Notes on `launch_polling_actors`:

- `reconcile_polling_actors` refuses to run without a reachable cluster.
  `launch_polling_actors --force` does not check, so running it where
  `RAY_ADDRESS` is unset starts a throwaway local Ray cluster in-process.
- Without `--force` it waits 60 seconds first and retries up to 10 times with
  a 60-second pause after each failure, so it can take about 11 minutes. It
  reuses existing healthy actors and replaces any that report unhealthy.
- It prints "Polling actors loaded successfully" even when every attempt
  failed. Check the health endpoint afterwards.
- `--force` does not touch `speculative_appeals_actor`.

The Ray dashboard is covered in [DEVELOPMENT_NOTES.md](../DEVELOPMENT_NOTES.md).
