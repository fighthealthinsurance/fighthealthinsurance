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
| Workers (our code) | `temporal-worker` Deployment (`worker.yaml`) | Runs `manage.py run_temporal_worker` on the existing app image. |
| Web UI | Temporal Web (chart `web.enabled`) | Optional; expose through the existing nginx ingress. |

Rough resource ask for the server at our scale: ~0.5–1 vCPU and ~1–2 GiB. That
fits comfortably next to the Ray heads (which already request 6 GiB each).

**Ray stays.** This is coexistence: Temporal takes over fax orchestration and
(later) the other polling/refresh actors; Ray keeps doing genuine ML work.

## One-time setup

1. **Create the databases and a user** on the existing Postgres:

   ```sql
   CREATE DATABASE temporal;
   CREATE DATABASE temporal_visibility;
   CREATE USER temporal WITH PASSWORD '...';
   GRANT ALL PRIVILEGES ON DATABASE temporal TO temporal;
   GRANT ALL PRIVILEGES ON DATABASE temporal_visibility TO temporal;
   ```

2. **Store the DB password** as a secret the chart reads:

   ```sh
   kubectl -n totallylegitco create secret generic temporal-postgres \
     --from-literal=password='...'
   ```

3. **Install the server** (the chart runs the schema setup job against Postgres):

   ```sh
   helm repo add temporal https://go.temporal.io/helm-charts
   helm repo update
   # Fill in TEMPORAL_PG_HOST / TEMPORAL_PG_USER in values.yaml first.
   helm install temporal temporal/temporal -n totallylegitco -f values.yaml
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

The repo's manifests have some drift between the pods that currently touch fax
documents; confirm these against the **live** namespace rather than trusting
any one yaml:

1. **Fax-document PVC** — the worker mounts `new-uploads-longhorn-backup4` at
   `/external_data` (matching the web pods in `deploy-back.yaml`, which write
   the documents). The Ray back cluster (`ray/cluster-back.yaml`) mounts
   `new-uploads-longhorn-backup3` at the same path — if that's the claim with
   the real documents in your cluster, change the worker's claim to match:

   ```sh
   kubectl -n totallylegitco get pvc | grep new-uploads
   ```

2. **Fax SSH secret** — the worker mounts `faxymcfaxface-ssh` (as
   `ray/cluster.yaml` does); `ray/cluster-back.yaml` uses `ssh-privatekey`
   instead. Check which secret actually exists / holds the working key:

   ```sh
   kubectl -n totallylegitco get secret | grep -E 'ssh|fax'
   ```

3. **SSH user** — the worker process runs as **root**, so it connects to the
   fax host as `root@$FAXYMCFAXFACE_HOST`. The Ray fax actor runs as the `ray`
   user and connects as `ray@...`. Make sure the fax host's `authorized_keys`
   accepts the key for the user the worker connects as (or add a `username=` to
   the SSH client config).

4. **Smoke test** — after deploying the worker but before flipping the flag on
   the web pods, exec into the worker pod and confirm it can read a stored
   document **and** authenticate to the fax host with the same user/key
   mapping the worker uses (local user + `~/.ssh` + `$FAXYMCFAXFACE_HOST`):

   ```sh
   kubectl -n totallylegitco exec deploy/temporal-worker -- ls /external_data | head
   kubectl -n totallylegitco exec deploy/temporal-worker -- \
     sh -c 'ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$FAXYMCFAXFACE_HOST" true'
   ```

   If the image has no `ssh` binary, use the bundled asyncssh instead:

   ```sh
   kubectl -n totallylegitco exec deploy/temporal-worker -- python -c \
     "import asyncio, os, asyncssh; asyncio.run(asyncssh.connect(os.environ['FAXYMCFAXFACE_HOST'])); print('ssh ok')"
   ```

5. **Drain the pre-flag backlog** — faxes queued before the flip
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

- `values.yaml` — Helm values: Postgres-backed, no Cassandra/Elasticsearch.
- `worker.yaml` — the `temporal-worker` Deployment.

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
