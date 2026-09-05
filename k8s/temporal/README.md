# Self-hosting Temporal on the FHI cluster

This directory holds everything needed to run Temporal **on the existing
Kubernetes cluster** (namespace `totallylegitco`) alongside the web pods and the
Ray cluster. No Temporal Cloud account required.

## Can we self-host on our current servers? Yes.

The footprint is modest because we reuse infrastructure we already run:

| Temporal needs | What we use | Notes |
| --- | --- | --- |
| A SQL datastore | **The existing PostgreSQL** | Add two databases: `temporal` and `temporal_visibility`. |
| Advanced visibility (search/list workflows) | **PostgreSQL 12+** | No Elasticsearch needed — Postgres ≥ 12 provides advanced visibility natively. This is the big saving on a small cluster. |
| Server services (frontend/history/matching/worker) | One combined Deployment via Helm | Start at `replicaCount: 1`; split/scale later. |
| Workers (our code) | `fhi-fax-worker` (`worker.yaml`) + `fhi-appeal-worker` (`appeal-worker.yaml`) Deployments | Both run `manage.py run_temporal_worker` on the existing app image; `TEMPORAL_WORKER_QUEUES` picks the role (`fax` / `appeal`), so the two queues share no failure domain. The appeal Deployment is safe to apply dark: with the journey flags off it idles and hosts nothing. (Named `fhi-*-worker` because the Helm chart itself owns a Deployment called `temporal-worker` — Temporal's internal worker service.) |
| Web UI | Temporal Web (chart `web.enabled`) | Optional; expose through the existing nginx ingress. |

Rough resource ask for the server at our scale: ~0.5–1 vCPU and ~1–2 GiB. That
fits comfortably next to the Ray heads (which already request 6 GiB each).

**Ray stays.** This is coexistence: Temporal takes over fax orchestration and
(later) the other polling/refresh actors; Ray keeps doing genuine ML work.

## One-time setup

1. **Create the databases and a user** on the existing Postgres:

   ```sql
   CREATE USER temporal WITH PASSWORD '...';
   CREATE DATABASE temporal OWNER temporal;
   CREATE DATABASE temporal_visibility OWNER temporal;
   ```

   Ownership (not `GRANT ALL`) is required: on PostgreSQL 15+ `GRANT ALL
   PRIVILEGES ON DATABASE` no longer allows creating tables in the `public`
   schema, so the chart's schema job would fail. `values.yaml` sets
   `createDatabase: false` accordingly — the databases must exist first.

2. **Store the DB password** as a secret the chart reads:

   ```sh
   kubectl -n totallylegitco create secret generic temporal-postgres \
     --from-literal=password='...'
   ```

3. **Install the server** (the chart runs the schema setup job against Postgres):

   ```sh
   helm repo add temporal https://go.temporal.io/helm-charts
   helm repo update
   # values.yaml has two ${...} placeholders Helm does NOT substitute
   # (TEMPORAL_PG_HOST is the CNPG read-write Service of the app Postgres):
   TEMPORAL_PG_HOST=fhi-pg-main-9-rw.totallylegitco.svc \
   TEMPORAL_PG_USER=temporal \
   envsubst '${TEMPORAL_PG_HOST} ${TEMPORAL_PG_USER}' < values.yaml > /tmp/temporal-values.yaml
   helm install temporal temporal/temporal --version 1.6.0 \
     -n totallylegitco -f /tmp/temporal-values.yaml
   ```

   The chart version is pinned to **1.6.0** — the version `values.yaml` was
   validated against on the live cluster. Newer chart releases may change the
   values layout again (pre-1.0 → 1.x did); re-validate before bumping the pin. The chart
   does not create the `default` namespace — after install:

   ```sh
   kubectl -n totallylegitco exec -i deploy/temporal-admintools -- \
     temporal operator namespace create --address temporal-frontend:7233 --retention 720h default
   ```

   This creates the in-cluster `temporal-frontend:7233` service the worker and
   the app connect to.

4. **Deploy the worker:**

   ```sh
   # ${FHI_BASE}/${FHI_VERSION} are substituted the same way as the other k8s/ manifests.
   envsubst < worker.yaml | kubectl apply -f -
   envsubst < appeal-worker.yaml | kubectl apply -f -
   kubectl apply -f worker-pdb.yaml
   # The PodMonitor and PrometheusRule need the Prometheus operator's CRDs.
   # scripts/build.sh skips each when its CRD is absent; do the same by hand
   # so these commands work on a cluster without the operator.
   kubectl get crd podmonitors.monitoring.coreos.com >/dev/null 2>&1 \
     && kubectl apply -f worker-podmonitor.yaml \
     || echo "no PodMonitor CRD -- worker metrics will not be scraped"
   kubectl get crd prometheusrules.monitoring.coreos.com >/dev/null 2>&1 \
     && kubectl apply -f worker-alerts.yaml \
     || echo "no PrometheusRule CRD -- worker alerts not installed"
   ```

## What `scripts/build.sh` does with these manifests

A normal prod deploy applies all of the above for you — `worker.yaml`,
`appeal-worker.yaml`, `worker-pdb.yaml`, both PodMonitors, both
PrometheusRules, `intake-outbox-cronjob.yaml` and
`backfill-fingerprints-job.yaml` — so the hand commands above are for a
first bring-up or a one-off repair, not for every release.

Three things to know before you run it:

- **It can wait a long time.** Worst case is about two hours: 15m per
  Deployment rollout (`web`, `fhi-fax-worker`, `fhi-appeal-worker`), then up
  to 10m each waiting for the old `web` and `fhi-appeal-worker` pods to
  actually exit, up to 25m for the Ray cluster (old pods gone, new pods
  appear, new pods Ready), then up to 30m for the strict fingerprint backfill
  Job. In practice it is far shorter — the drains are mostly done by the time
  the rollouts report, and the backfill on an already-clean table takes a
  couple of minutes.
- **Rolling out is not the same as draining, and the script waits for both.**
  `kubectl rollout status` returns as soon as the new replicas are available
  and the old ones stop being *counted* — a pod stops counting the moment it
  is marked for deletion, while its process keeps running preStop and
  finishing in-flight work. `web` allows 420s plus a 15s preStop and
  `fhi-appeal-worker` 360s, and both write `ProposedAppeal` rows, so the
  script additionally waits for those pods to be gone before the strict
  backfill runs. `fhi-fax-worker` is rolled but deliberately not drained: it
  polls the fax queue only, writes no `ProposedAppeal`, and its 1860s grace
  period would add half an hour to every deploy for nothing.
- **A stalled rollout or a failed backfill Job ends the deploy non-zero, on
  purpose.** The Job failing means it found rows a pre-fingerprint writer
  produced; look at it before rerunning:

  ```sh
  kubectl -n totallylegitco logs job/fhi-backfill-appeal-fingerprints
  ```

  Every apply happens before every wait, so a failure here still leaves the
  new image, the PDBs, the monitors, the alerts and the relay CronJob in
  place.

Two escape hatches when the waiting is not what you want:

| Flag | Effect |
| --- | --- |
| `--skip-journey-gates` | Applies everything including the backfill Job, but blocks on nothing — no rollout wait, no drain, no Job wait. You check the Job yourself. |
| `--skip-backfill` | Does not apply or wait for the backfill Job, nor for the writer drain that only that Job needs. Rollout waits still run, because a Deployment that never finishes rolling is a broken deploy either way. |

Neither changes what image ships. What the gate buys you, if you skip it:
`ProposedAppeal.text_fingerprint` can carry NULLs or stale values written by a
pod that had not finished draining, which only matters once the journey flags
are on.

## Turning it on

Fax sending only routes through Temporal when `TEMPORAL_ENABLED=true`. Until
then everything stays on the Ray fax actor, so this can be deployed dark and
flipped on later.

Set `TEMPORAL_ENABLED=true` in the **shared `fight-health-insurance-secret`**,
which the web pods, the Ray cluster pods, and the worker all read via `envFrom`.
Do **not** set it only on the web pods: the Ray `FaxPollingActor` sweep gates
itself off by reading `TEMPORAL_ENABLED` *in its own process*, so if the Ray
pods don't see the flag the sweep keeps running and races the Temporal delay
timer on the same paid fax (both would try to send it). The atomic
`vendor_send_completed` claim in `fax_send_core` prevents an actual double
transmission, but the correct configuration is to flip the flag everywhere at
once via the shared secret.

| Env var | Value | Where |
| --- | --- | --- |
| `TEMPORAL_ENABLED` | `true` | shared `fight-health-insurance-secret` (web + Ray + worker) |
| `TEMPORAL_HOST` | `temporal-frontend:7233` | web + worker |
| `TEMPORAL_NAMESPACE` | `default` | web + worker |
| `TEMPORAL_TASK_QUEUE` | `fhi-fax` | web + worker |

(The worker Deployment also sets `TEMPORAL_ENABLED=true` explicitly, so it is
always on regardless of the shared secret.)

`TEMPORAL_TLS=true` enables (server-side) TLS to the cluster. For **mTLS**,
additionally set **both** `TEMPORAL_CLIENT_CERT_PATH` and
`TEMPORAL_CLIENT_KEY_PATH` — if either is missing the client silently falls
back to plain TLS.

## Pre-flight checklist (verify before flipping the flag)

1. **Fax documents** — the worker deliberately mounts **no** documents PVC:
   fax activities read stored PDFs through the external `COMBINED_STORAGE`
   layers (object storage, credentials via the shared secrets) and regenerate
   the document if it isn't found. Confirm the worker's boot log shows the
   external storage check passing.

2. **Fax SSH key + user** — the worker mounts the `faxymcfaxface-ssh` secret
   with the key renamed to `id_ed25519` (SSH libraries only auto-offer keys
   with standard filenames — a `ssh-auth` secret's raw `ssh-privatekey` name
   is never searched), and sets `USER`/`LOGNAME=ray` so asyncssh/paramiko
   connect as the account the fax host trusts even though the process runs as
   root. If reusing this pattern elsewhere, keep both halves: standard key
   filename **and** the right username.

3. **Smoke test** — after deploying the worker but before flipping the flag,
   exercise the exact library calls the fax code makes:

   ```sh
   kubectl -n totallylegitco exec deploy/fhi-fax-worker -- python -c \
     "import asyncio, os, asyncssh; \
      asyncio.new_event_loop().run_until_complete(asyncssh.connect(os.environ['FAXYMCFAXFACE_HOST'], known_hosts=None)); \
      print('ssh ok')"
   ```

   Note this is a **connectivity + authentication check only**: `known_hosts=None`
   mirrors what `fax_utils` itself does for this VPN-internal host, and means
   neither the code nor this test validates the server's identity. If that
   posture ever changes in `fax_utils`, mount a trusted `known_hosts` (e.g.
   from a secret) and point both at it. (The plain `ssh` binary ignores `USER`,
   so for a CLI check use `ssh ray@$FAXYMCFAXFACE_HOST` explicitly.)

4. **Drain the pre-flag backlog** — faxes queued before the flip
   (`should_send=True, sent=False`) have no Temporal workflow. The Ray delayed
   sweep only goes idle in a process that actually sees `TEMPORAL_ENABLED=true`,
   so this holds **only if the flag is set on the Ray pods** (via the shared
   secret above) — otherwise the sweep keeps running alongside Temporal. Before
   (or right after) flipping, send any stragglers via
   `SendFaxHelper.blocking_dosend_all` or confirm none exist. Also kill the old
   detached `fax_polling_actor` if one is still running from a pre-flag deploy
   (`launch_polling_actors --force` relaunch excludes it under Temporal, but
   does not kill a live one).

5. **Worker observability (review-9)** — before relying on the alert
   rules in `worker-alerts.yaml`, verify against the LIVE Prometheus:
   the PodMonitor selector actually produces `up{pod=~"fhi-(fax|appeal)-worker-.*"}`
   targets (operator `podMonitorSelector` caveat), kube-state-metrics is
   scraped (`kube_deployment_status_replicas_available`), the SDK series
   carry `namespace="default"` (the Temporal namespace, not the Kubernetes
   one), and every SERVER-side metric name in the `fhi-temporal-server-side`
   group exists on the deployed Temporal server version (`approximate_backlog_count`,
   `task_schedule_to_start_latency`, `activity_timeout`) with the units the
   thresholds assume. Worker-side series vanish when the last worker dies,
   so only the worker-loss and server-side rules can page on "zero workers";
   promote their severity once verified.

## Applying a values.yaml change

Editing `values.yaml` does **not** change anything by itself, and neither does
restarting the Temporal pods — Kubernetes re-reads the ConfigMap Helm already
rendered, not this file. Changes land only via `helm upgrade`:

```sh
TEMPORAL_PG_HOST=fhi-pg-main-9-rw.totallylegitco.svc \
TEMPORAL_PG_USER=temporal \
envsubst '${TEMPORAL_PG_HOST} ${TEMPORAL_PG_USER}' < values.yaml > /tmp/temporal-values.yaml
helm upgrade temporal temporal/temporal --version 1.6.0 \
  -n totallylegitco -f /tmp/temporal-values.yaml
```

Datastore changes restart the four Temporal server pods, so the cluster is
briefly unavailable (~30-60s). FHI's own pods (web, `fhi-fax-worker`) are not
restarted, but a fax dispatched during that window fails over to the Ray path
— pick a quiet moment. Verify after:

```sh
kubectl -n totallylegitco exec -i deploy/temporal-admintools -- \
  temporal operator cluster health --address temporal-frontend:7233
```

## Web UI

The Temporal Web UI (`temporal-web`, a ClusterIP Service) has no auth of its
own and gets no ingress. Staff reach it at **`/timbit/temporal/`** on the app,
which is a read-only reverse proxy in Django (`TemporalUIProxyView` in
`staff_views.py`) behind the usual `staff_member_required` login. Two chart
env vars in `values.yaml` make that work: `TEMPORAL_UI_PUBLIC_PATH` so the
UI's assets and API calls use the proxied prefix, and
`TEMPORAL_DISABLE_WRITE_ACTIONS` so the UI offers no terminate/signal/reset
buttons (the proxy also forwards only GET/HEAD). Changing either is a
`helm upgrade` (above). Direct access for debugging still works with
`kubectl -n totallylegitco port-forward svc/temporal-web 8080:8080`, but note
the UI then expects to be served under the public path, i.e.
`http://localhost:8080/timbit/temporal/`.

## Rollback

Set `TEMPORAL_ENABLED=false` (or scale the worker to 0). Fax dispatch falls
straight back to the Ray path — no code change or redeploy of the app image
required.

## Files

- `values.yaml` — Helm values (validated against chart `temporal-1.6.0`, which
  the install command pins): Postgres-backed, no Cassandra/Elasticsearch.
- `worker.yaml` — the `fhi-fax-worker` Deployment.
- `appeal-worker.yaml` — the `fhi-appeal-worker` Deployment (dark-safe; idles until the journey flags flip).
- `worker-pdb.yaml` — PodDisruptionBudgets (minAvailable: 1) for both worker Deployments.
- `worker-podmonitor.yaml` — scrapes the SDK's Prometheus endpoint (`TEMPORAL_METRICS_BIND`, port 9464) on both workers.
- `worker-alerts.yaml` — PrometheusRule: schedule-to-start latency, slot exhaustion, activity failures, frontend RPC failures.

## What runs here today vs. next

- **Now:** `SendFaxWorkflow` (immediate fax send; durable 1-hour delay timer
  available via `delay_send`).
- **Next:** point fax creation at `delay_send=True` to retire the
  `FaxPollingActor`; then convert the refresh/prefetch actors to Temporal
  Schedules and the email-polling actor to a workflow. See the migration notes
  in the PR description.

> **HIPAA note:** workflow inputs/outputs here are deliberately limited to a
> hashed email + fax uuid + booleans — **no PHI** is written to Temporal
> history. Any future workflow that must carry PHI should add an encryption
> `PayloadCodec` (see the Temporal data-handling reference) before doing so.

## Protecting user data in workflow history

Workflow payloads are claim-check style by contract: opaque
`(hashed_email, uuid)` identifiers only, never case content (enforced by
`tests/temporal/test_temporal_codec.py`). Two further layers:

- **Retention is the storage bound.** The namespace is created with
  `--retention 720h`, so closed-workflow histories are deleted by the
  server after 30 days. History is a debugging window, not a data store
  (the durable data lives in Django); raise it deliberately, not by
  default.
- **Payload encryption.** Set `TEMPORAL_PAYLOAD_KEY` (a Fernet key) in the
  app/worker environment and every payload is encrypted client-side
  (`fighthealthinsurance/temporal_codec.py`): the Temporal database and UI
  hold ciphertext only. Destroying or rotating the key renders all
  existing histories unreadable at once, an immediate backstop if any
  history ever needs to be expunged ahead of retention. Decoding passes pre-key plaintext histories
  through, so the key can be introduced without draining workflows.
