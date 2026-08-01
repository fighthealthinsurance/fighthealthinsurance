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
| Workers (our code) | `fhi-fax-worker` Deployment (`worker.yaml`) | Runs `manage.py run_temporal_worker` on the existing app image. (Named `fhi-fax-worker` because the Helm chart itself owns a Deployment called `temporal-worker` — Temporal's internal worker service.) |
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
   ```

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

## Rollback

Set `TEMPORAL_ENABLED=false` (or scale the worker to 0). Fax dispatch falls
straight back to the Ray path — no code change or redeploy of the app image
required.

## Files

- `values.yaml` — Helm values (validated against chart `temporal-1.6.0`, which
  the install command pins): Postgres-backed, no Cassandra/Elasticsearch.
- `worker.yaml` — the `fhi-fax-worker` Deployment.

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
