# DB-host configurability refactor (pg8 -> pg9 cutover)

Makes the production Postgres **host** a single, version-controlled, reversible
value so the `fhi-pg-main-8` -> `fhi-pg-main-9` cutover flips one thing instead
of hand-editing opaque cluster Secrets.

## What the app actually reads

`fighthealthinsurance/settings.py` (`Prod.DATABASES`, line ~818) reads the host
from an env var:

```python
"HOST": os.getenv("PDBHOST"),
```

Username/password/name come from `PDBUSER` / `PDBPASSWORD` / `PDBNAME`
(unchanged, still Secret-only — never moved into a ConfigMap).

## Where the host came from BEFORE

There were **no hard-coded `fhi-pg-main-8*` hostnames anywhere in the repo.**
`PDBHOST` was injected only through the cluster Secrets `fight-health-insurance-secret`
and `fight-health-insurance-primary-secret` (referenced via `envFrom`), which are
**not** in git. The host was therefore invisible to review and could only be
changed by editing a live Secret.

## The refactor

New ConfigMap `k8s/db-config.yaml` (`fhi-db-config`) holds `PDBHOST`, defaulting
to the **current** value `fhi-pg-main-8-rw.totallylegitco.svc`. Every
DB-consuming production workload now takes `PDBHOST` as an explicit container
`env` entry with `valueFrom.configMapKeyRef`.

Kubernetes gives a container-level `env` entry precedence over any `envFrom`
(Secret) value for the same key, so the ConfigMap is authoritative for the host
while passwords stay in the Secrets. Default == current host => **applying this
changes nothing at runtime.**

## Every reference parameterized

| File | Workload | Change |
|---|---|---|
| `k8s/db-config.yaml` | ConfigMap `fhi-db-config` (new) | Declares `PDBHOST` = `fhi-pg-main-8-rw.totallylegitco.svc` |
| `k8s/deploy.yaml` | Job `web-migrations` | Added explicit `PDBHOST` env from ConfigMap |
| `k8s/deploy.yaml` | Job `web-actor-launch` | Added explicit `PDBHOST` env from ConfigMap |
| `k8s/deploy.yaml` | Job `web-extralink-prefetch` | Added explicit `PDBHOST` env from ConfigMap |
| `k8s/deploy.yaml` | Deployment `web` | Added explicit `PDBHOST` env from ConfigMap |
| `k8s/ray/cluster.yaml` | RayCluster head (`ray-head`) | Added explicit `PDBHOST` env from ConfigMap |
| `k8s/ray/cluster.yaml` | RayCluster worker (`ray-worker`) | Added explicit `PDBHOST` env from ConfigMap |

These are exactly the production workloads that talk to the primary DB: the web
tier, the scheduled/migration/actor/prefetch Jobs, and the Ray background
workers.

### Deliberately NOT changed (with reasons)

- **`k8s/deploy_staging.yaml`** (`web-staging`, `web-migrations-staging`) — staging's
  effective `PDBHOST` comes from the same Secrets but its target DB is not
  verifiable from the repo. Forcing it onto `fhi-db-config` (which defaults to the
  **prod** -8 primary) could silently repoint staging at the prod database. The
  cutover is prod-only, so staging is left untouched. If staging shares the -8
  primary and should follow the cutover, add the same `PDBHOST` env block +
  a staging-scoped ConfigMap — do it as a separate, explicitly-decided change.
- **`k8s/deploy_dev.yaml`** (`web-dev`) — dev uses SQLite (`DEV_DB_LOC=/tmp/db.sqlite3`),
  not Postgres. No `PDBHOST` involved.
- **`k8s/ray/cluster-back.yaml`** — stale `-back` backup copy, not applied by
  the deploy pipeline. Left as-is to avoid drift; update it only if it is ever
  reactivated. (The equivalent `k8s/deploy-back.yaml` and
  `k8s/deploy_staging-back.yaml` copies have been deleted — they defined the
  same `web`/`web-staging` Deployment names as the live manifests but with
  pre-incident config, so an accidental `kubectl apply` would have clobbered
  the live Deployments; for prod `web` it would also have repointed `PDBHOST`
  at the old secrets-sourced database host (staging is secrets-sourced either
  way, see above). The uploads PVC they carried now lives in
  `k8s/uploads-pvc.yaml`.)
- **`pg-*.yaml`, `k8s/deploy.yaml` service names** — the remaining legacy
  `fhi-pg*` references are CNPG **Cluster manifests** (`pg-copy.yaml`,
  `pg-recover.yaml`) naming clusters/buckets, and app **Service** names
  (`web-svc` etc). Neither is an app DB host; not touched. The live host
  endpoints in `k8s/db-config.yaml` and `scripts/cutover-app-to-pg9.sh` are
  intentional — they are the config/cutover point this doc describes.
  (`pg-bootstrap-raw.yaml`, the old -7 bootstrap, has since been deleted
  outright.)

## Reversibility

Two independent, cheap reversals:

1. **Runtime host** (what the cutover flips) — patch the ConfigMap back and roll:
   ```bash
   kubectl -n totallylegitco patch configmap fhi-db-config --type=merge \
     -p '{"data":{"PDBHOST":"fhi-pg-main-8-rw.totallylegitco.svc"}}'
   kubectl -n totallylegitco rollout restart deployment/web
   kubectl -n totallylegitco rollout restart deployment/web-staging  # only if wired
   ```
   `scripts/cutover-app-to-pg9.sh` writes this exact reverse patch to disk during
   cutover; `scripts/rollback-pre-write-cutover.sh` applies it (pre-write only).

2. **The manifest refactor itself** — pure git revert of this PR. Because the
   ConfigMap default equals the pre-existing host, reverting is a no-op at runtime.
