# Runbook — migrate `fhi-pg-main-8` → `fhi-pg-main-9` (CloudNativePG, logical dump/restore)

**Single, master, gated runbook** for standing up **`fhi-pg-main-9`** as a fresh,
empty CloudNativePG **primary**, verifying its backups, then seeding it from
**`fhi-pg-main-8`** with a **logical dump/restore** (`pg_dump` → `pg_restore`)
during a short cutover maintenance window — while never interrupting the only
healthy production primary (`fhi-pg-main-8-3`).

> **Why dump/restore and not a streaming clone?** `-8`'s pgdata is ~229GB, and
> **almost all of it is stuck, unarchivable WAL**, not table data. A physical
> clone would ship that whole mess (and inherit its retention problems). A
> logical dump copies only the **actual app data** — small and fast — and sidesteps
> the "huge file" problem entirely. `-9` therefore never physically clones `-8`.

> **Every step is `ACTION → VALIDATION → GATE`. Do NOT proceed past a step until
> its own validation is green.** Commands are copy-pasteable. The cutover window
> (Phases 5–9) is the only phase with app downtime; everything before it is a
> safe no-op against production.

Deep dives: [`docs/pg-reliability-hardening.md`](./pg-reliability-hardening.md)
(why `-8` broke + how backups are hardened) and
[`k8s/db-host-configurability.md`](../k8s/db-host-configurability.md) (how the app
DB host became one reversible switch).

---

## At-a-glance — the whole migration in order

Run top to bottom. Each phase has its own gate; a red gate stops the line.
**Phases 0–4 are zero-downtime** and can be done days ahead. **Phases 5–9 are the
cutover maintenance window** (app is down). Phases 10–11 are post-cutover.

| # | Phase | One-line goal | Green gate |
|---|---|---|---|
| **Pre-window (no downtime)** ||||
| 0 | [`-8` hygiene](#phase-0--8-hygiene-optional-but-recommended) | Shrink `-8` bloat + verify `-8`'s own backups | `-8` stable; stale slots dropped |
| 1 | [Parameterize app DB host](#phase-1--parameterize-the-app-db-host-safe-no-op-now-the-cutover-switch-later) | Install the `PDBHOST` switch (no-op today) | app still on `-8`; ConfigMap = current host |
| 2 | [Provision empty `-9`](#phase-2--provision-the-empty--9-primary) | ObjectStore + fresh-primary Cluster | `-9-1` Running, **writable primary**, empty |
| 3 | [Verify `-9` backups](#phase-3--verify--9-backups-while-empty) | `-9`'s OWN archiver + first backup work | `-9` Backup `completed`; objects in bucket |
| 4 | [Scale `1→2→3`](#phase-4--scale-123) | Add healthy `-9` HA replicas | each replica ready + sidecar + bounded lag |
| **Cutover window (app downtime)** ||||
| 5 | [Quiesce `-8` writes](#phase-5--cutover-window-quiesce--8-writes) | App maintenance / writers to 0 | `-8` has no active app backends |
| 6 | [Dump `-8`](#phase-6--dump--8) | Logical dump of the app data | validated dump in hand |
| 7 | [Restore into `-9`](#phase-7--restore-into--9) | `pg_restore` globals + app DB | restore validated; tables present |
| 8 | [Data validation](#phase-8--data-validation-8-vs-9) | Compare `-8` vs `-9` (writes stopped) | `DATA VALIDATION PASSED` |
| 9 | [Flip `PDBHOST` → `-9`](#phase-9--flip-pdbhost---9-cutover-the-app) | Cut the app over to `-9` | app healthy on `-9`; `-8` clientless |
| **Post-cutover** ||||
| 10 | [Rollback semantics](#phase-10--rollback-semantics) | Know how to reverse | exactly one writable primary always |
| 11 | [Backup + WAL-bounded verification](#phase-11--backup--wal-bounded-verification-do-this-before-trusting--9) | Prove `-9` is durably backed & WAL can't grow | fresh backup + **restore drill** + **WAL alert** live |
| — | [Decommission `-8`](#decommission--8-only-after-a-verified--9-restore) | Only after a verified `-9` restore drill | all of Phase 11 green |

---

## Hard environment facts

| Thing | Value |
|---|---|
| Namespace | `totallylegitco` |
| Source cluster | `fhi-pg-main-8` (healthy primary: `fhi-pg-main-8-3`; `-8-2` is **suspect**; **no `-8-1`**) |
| Source rw service | `fhi-pg-main-8-rw.totallylegitco.svc` |
| Target cluster | `fhi-pg-main-9` (rw service `fhi-pg-main-9-rw.totallylegitco.svc`) — **fresh primary** |
| PG image | `ghcr.io/cloudnative-pg/postgresql:18.1-system-trixie` |
| Operator | CloudNativePG `v1.28.0` |
| Backup plugin — current `-8` | barman-cloud sidecar `v0.9.0` (rolled back to recover from the outage) |
| Backup plugin — target `-9` | barman-cloud `v0.13.0` (`…/plugin-barman-cloud-sidecar:v0.13.0`) — deliberate re-upgrade; apply §2/§3 hardening first |
| Storage class | `encrypted-local-path` (RWO) |
| App DB / owner | `app` / `ziggystardust` |
| App secret | `fhi-internal-pg-secret` (owner password) |
| Superuser secret | `fhi-superuser-pg-secret` |
| Backup creds | `pg-backup2` (`PG_ACCESS_KEY_ID` / `PG_ACCESS_SECRET_KEY`) |
| Bucket / endpoint | `s3://fhi-pg-backup-second/` @ `https://s3.us-west-004.backblazeb2.com` |
| -9 ObjectStore | `fhi-backup-store-9`, serverName `fhi-pg-main-9` |
| App DB-host switch | ConfigMap `fhi-db-config`, key `PDBHOST` (the single cutover flip) |

> **No migration replication role is needed.** The dump runs as `postgres` over
> each pod's **local socket** (`pg_dump -U postgres`), so there is no network
> replication role, no `pg_hba` `replication` line, and no `wal_keep_size` clone
> window to size. This is a major simplification versus a streaming clone.

## Deliverables (all in this branch)

Manifests:

- `k8s/fhi-pg-main-9-objectstore.yaml` — `-9`'s isolated Barman ObjectStore (serverName `fhi-pg-main-9`, 30d retention).
- `k8s/fhi-pg-main-9-cluster.yaml` — the `-9` **fresh-primary** Cluster (initdb, no streaming).
- `k8s/fhi-pg-main-9-alerts.yaml` — PrometheusRule: **WAL >20GB**, backup age, archiver failing.
- `k8s/db-config.yaml` — `fhi-db-config` ConfigMap holding `PDBHOST` (the cutover switch).
- `k8s/deploy.yaml`, `k8s/ray/cluster.yaml` — prod workloads wired to `PDBHOST` from that ConfigMap.

Scripts (all `set -euo pipefail`, context-checked, namespace-defaulted, password-safe):

- `scripts/backup-pg8-live-base.sh` — logical dump of `-8` (the migration seed).
- `scripts/restore-pg9-from-dump.sh` — restore the dump into the empty `-9` primary.
- `scripts/validate-pg8-vs-pg9.sh` — data consistency `-8` vs `-9`.
- `scripts/cutover-app-to-pg9.sh` — flip `PDBHOST` → `-9`, roll the app.
- `scripts/rollback-pre-write-cutover.sh` — pre-write rollback of the cutover.
- **Optional `-8` hygiene (not on the migration critical path):**
  `scripts/check-pg8-source.sh`, `scripts/set-pg8-wal-retention.sh`,
  `scripts/create-or-fix-pg9-migration-role.sh`, `scripts/check-pg9-replication.sh`.
  These were built for the streaming-clone approach; under dump/restore they are
  useful only for `-8` self-health and `-9` sidecar/HA-replica checks. Their
  "`-9` streams from `-8`" assertions **do not apply** here.

Docs: this file, `docs/pg-reliability-hardening.md`, `k8s/db-host-configurability.md`.

---

## ⚠️ WHAT NOT TO DO (read first)

- **Never** `kubectl delete pod fhi-pg-main-8-3` or delete its PVC — it is the
  ONLY healthy primary; losing it loses the database.
- **Do not delete `fhi-pg-main-8-2`** either. It is a suspect replica
  (`1/2 Running`, thousands of restarts); deleting it can leave you on a single
  pod if its rebuild stalls. There is **no `-8-1`** (it died disk-full).
- **Never reuse `serverName: fhi-pg-main-8`** for `-9`'s ObjectStore/backups —
  it would corrupt/overwrite `-8`'s backup lineage. `-9` uses `fhi-pg-main-9`.
- **Take the dump only after `-8` writes are quiesced** (Phase 5). A dump taken
  while the app is writing captures a moving target and loses the delta.
- **Never restore over existing data.** `restore-pg9-from-dump.sh` refuses if the
  target `app` DB already has user tables (override only with `FORCE=true`).
- **Never allow writes to BOTH clusters at once** (split brain). Exactly one
  writable primary at all times. After cutover, keep `-8` clientless.
- **Do not manually create/delete CNPG-managed pods** (`fhi-pg-main-9-*`). Let the
  operator own them; scale via `spec.instances`.
- **Do not re-attempt the v0.9→v0.13 plugin upgrade blind.** That upgrade caused
  the `-8` outage. Apply the colo-scripts Service-selector + CA fixes (hardening
  doc §2/§3) first, run a **single** pinned `v0.13.x`, and confirm the plugin
  Service EndpointSlice is non-empty.
- **Never** put a generated password in a committed manifest, a log line, or a
  command-line argument.

---

## Phase 0 — `-8` hygiene (optional but recommended)

**Why (and why it is no longer a hard gate):** the dump/restore seed does **not**
depend on `-8`'s object-store archive at all, so a dead `-8` archiver no longer
blocks the migration. But `-8` remains your **rollback target** until decommission,
so it should have its own working backups and should not be sitting on 229GB of
stuck WAL. This phase is `-8` self-care — do it if time allows; it does not gate `-9`.

> **DO NOT delete `-8-3` or `-8-2`.** Use only the non-destructive SQL path below.
> If the plugin itself is unreachable, the full P0 plugin hotfix (Service selector
> + leader lease + CA) lives in the hardening doc; that is a `-8` incident task,
> separate from this migration.

**ACTION** — drop stale slots (the top cause of the bloat) and nudge the archiver,
without touching any pod:

```bash
# inspect which plugin image each pod runs (records the current version = v0.9.0)
for pod in fhi-pg-main-8-2 fhi-pg-main-8-3; do
  kubectl -n totallylegitco get pod "$pod" \
    -o jsonpath='{.metadata.name}: {range .spec.initContainers[*]}{.name}={.image} {end}{"\n"}' 2>/dev/null
done

# drop STALE/inactive replication slots retaining WAL for dead -8 replicas
# (a leftover -8-1 slot is a prime suspect for the pgdata bloat):
kubectl -n totallylegitco exec fhi-pg-main-8-3 -c postgres -- psql -U postgres -c \
 "SELECT slot_name, active, pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(),restart_lsn)) retained
    FROM pg_replication_slots WHERE NOT active ORDER BY retained DESC;"
# then drop only confirmed-stale ones:
#   SELECT pg_drop_replication_slot('<stale_slot_name>');

# force WAL to advance (SQL only; does NOT restart -3):
kubectl -n totallylegitco exec fhi-pg-main-8-3 -c postgres -- psql -U postgres -c 'SELECT pg_switch_wal();'
```

**VALIDATION**

```bash
kubectl -n totallylegitco exec fhi-pg-main-8-3 -c postgres -- psql -U postgres -xc \
 "SELECT archived_count, failed_count, last_archived_time FROM pg_stat_archiver;"
# optional deeper source check (ignores its streaming-specific gates):
./scripts/check-pg8-source.sh || true
```

**GATE:** `-8` is stable, stale slots are gone, and pgdata usage is trending
**down** (or at least not climbing). This gate is about `-8`'s health as a
rollback target — it does **not** block provisioning `-9`.

---

## Phase 1 — Parameterize the app DB host (safe no-op now, the cutover switch later)

**Why now:** the app's Postgres host is read from `PDBHOST`
(`settings.py` → `os.getenv("PDBHOST")`). We introduce one version-controlled
switch — the `fhi-db-config` ConfigMap — and wire every DB-consuming prod workload
to read `PDBHOST` from it via an explicit container `env` entry (which takes
precedence over the `envFrom` Secret value for the same key). **The ConfigMap
defaults to the current `-8` host, so applying this now changes nothing** — it
just pre-positions the flip that Phase 9 will use. Rationale + the
"deliberately not changed" list: [`k8s/db-host-configurability.md`](../k8s/db-host-configurability.md).

**ACTION**

```bash
kubectl apply -f k8s/db-config.yaml        # defaults PDBHOST = fhi-pg-main-8-rw...
./scripts/build.sh --no-build
```

**VALIDATION** — confirm it was a true no-op (still pointing at `-8`):

```bash
kubectl -n totallylegitco get configmap fhi-db-config -o jsonpath='{.data.PDBHOST}{"\n"}'
kubectl -n totallylegitco get pods -l app=fight-health-insurance-prod -o name | head -1 | \
  xargs -I{} kubectl -n totallylegitco exec {} -- printenv PDBHOST
kubectl -n totallylegitco rollout status deployment/web --timeout=300s
```

**GATE:** `PDBHOST` in both the ConfigMap and a running web pod is
`fhi-pg-main-8-rw.totallylegitco.svc`, and `deployment/web` is healthy. **Do NOT
change `PDBHOST` yet** — the flip to `-9` happens only in Phase 9.

---

## Phase 2 — Provision the empty `-9` primary

**ACTION** (order matters — the ObjectStore must exist before the Cluster that
references it):

```bash
kubectl apply -f k8s/fhi-pg-main-9-objectstore.yaml
kubectl -n totallylegitco get objectstore fhi-backup-store-9      # exists

kubectl apply -f k8s/fhi-pg-main-9-cluster.yaml                   # initdb, fresh primary
kubectl apply -f k8s/fhi-pg-main-9-alerts.yaml                    # WAL/backup alerts
```

**VALIDATION** — `-9` bootstraps as an **empty primary** (initdb), not a replica:

```bash
kubectl -n totallylegitco get cluster fhi-pg-main-9 -w
kubectl -n totallylegitco exec fhi-pg-main-9-1 -c postgres -- psql -U postgres -Atc \
 "SELECT pg_is_in_recovery();"                                    # want 'f' (a primary)
# the app db + owner exist, and are EMPTY (no user tables yet):
kubectl -n totallylegitco exec fhi-pg-main-9-1 -c postgres -- psql -U postgres -d app -Atc \
 "SELECT count(*) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema');"  # want 0
```

**GATE:** `fhi-pg-main-9-1` is `Running`, `pg_is_in_recovery()=f` (writable
primary), the `app` database exists owned by `ziggystardust`, and it has **0** user
tables (a clean seed target). The `plugin-barman-cloud-sidecar:v0.13.0` sidecar is
present in the pod.

### Troubleshooting — `fhi-pg-main-9-1` stuck `Pending` (encrypted-local-path PVC not binding)

A common failure here is the pod sitting `Pending` with its PVC unbound:

```
fhi-pg-main-9-1   Pending   ...   encrypted-local-path   <unset>   2m
```

`encrypted-local-path` is a **local** provisioner, almost always
`volumeBindingMode: WaitForFirstConsumer` — so the PVC will not bind until the pod
is **scheduled to a node**, and the volume is then created **on that node**. If the
pod can't be scheduled, the PVC stays `Pending` forever (and vice-versa). Walk it
top-down:

```bash
# 1. WHY is the PVC pending? read its events.
kubectl -n totallylegitco describe pvc fhi-pg-main-9-1 | sed -n '/Events/,$p'
#   "waiting for first consumer to be created before binding"  -> NORMAL; the
#      problem is pod SCHEDULING (go to step 2).
#   "no volume plugin matched" / provisioner errors            -> SC/provisioner
#      problem (steps 3-4).

# 2. WHY can't the pod schedule? read its events.
kubectl -n totallylegitco describe pod fhi-pg-main-9-1 | sed -n '/Events/,$p'
#   Look for FailedScheduling reasons:
#     "Insufficient cpu/memory"                  -> no node has room; free capacity
#     "volume node affinity conflict"            -> the PVC/volume is pinned to a
#         node that no longer fits the pod (or a stale PV from a prior attempt)
#     "didn't match pod anti-affinity rules" /
#     "had taint {..}, that the pod didn't tolerate" -> placement blocked

# 3. Does the StorageClass exist and is it WaitForFirstConsumer?
kubectl get storageclass encrypted-local-path -o \
  custom-columns=NAME:.metadata.name,PROV:.provisioner,BIND:.volumeBindingMode
#   -8 uses this same SC, so it should exist. If BIND=WaitForFirstConsumer, the
#   PVC WILL show Pending until the pod is placed -- that alone is not the bug.

# 4. Is the local-path provisioner actually running? (it creates the dir on-node)
kubectl get pods -A | grep -i local-path
kubectl -n local-path-storage logs -l app=local-path-provisioner --tail=50 2>/dev/null || \
  echo "adjust namespace/label to your provisioner's deployment"
#   A crashed/absent provisioner = PVCs never provision. Restart it if needed.

# 5. Node capacity / taints (local-path needs room on the TARGET node's disk):
kubectl get nodes
kubectl describe node <node> | sed -n '/Taints/,/Allocated/p'
#   Confirm a schedulable node has enough DISK for the 50Gi request AND no taint
#   the pod can't tolerate. On this cluster -3 lives on 'plushy', -2 on 'turo'.
```

**Most common causes here, in order:**
1. **`WaitForFirstConsumer` + an unschedulable pod** — the real blocker is
   scheduling (step 2), not storage. Fix the scheduling reason and the PVC binds.
2. **Stale PV/PVC from a prior apply** pinning the volume to a full/wrong node —
   delete the orphaned PVC **only for `-9`** (never `-8`'s) and let CNPG recreate:
   `kubectl -n totallylegitco delete pvc fhi-pg-main-9-1` (safe: `-9` is empty here).
3. **Provisioner down** (step 4) — restart the local-path provisioner deployment.
4. **No node with 50Gi free** — free disk or lower the `storage:` request in
   `k8s/fhi-pg-main-9-cluster.yaml` (it is resizable later).

**GATE (recovery):** after the fix, `kubectl -n totallylegitco get pvc,pod -l
cnpg.io/cluster=fhi-pg-main-9` shows the PVC `Bound` and the pod `Running`. Then
re-run the Phase 2 validation.

---

## Phase 3 — Verify `-9` backups (while empty)

**Why now:** prove `-9`'s **own** backup path works **before** it holds data, so a
backup failure is caught while it is harmless. `-9` is a primary from birth, so it
archives immediately (no standby/`always` ambiguity).

**ACTION**

```bash
kubectl -n totallylegitco exec fhi-pg-main-9-1 -c postgres -- psql -U postgres -c 'SELECT pg_switch_wal();'
sleep 20
kubectl -n totallylegitco cnpg backup fhi-pg-main-9
kubectl -n totallylegitco get backup -l cnpg.io/cluster=fhi-pg-main-9 -w
```

**VALIDATION**

```bash
kubectl -n totallylegitco exec fhi-pg-main-9-1 -c postgres -- psql -U postgres -xc \
 "SELECT archived_count, failed_count, last_archived_time FROM pg_stat_archiver;"
# objects landed under -9's OWN prefix (never -8's):
aws --endpoint-url https://s3.us-west-004.backblazeb2.com s3 ls \
  s3://fhi-pg-backup-second/fhi-pg-main-9/ | tail
```

**GATE:** a `Backup` for `fhi-pg-main-9` reaches `phase=completed`, `archived_count`
is rising with `failed_count` flat, and objects appear under
`s3://fhi-pg-backup-second/fhi-pg-main-9/`. If `failed_count` rises, stop and check
the B2 creds + the checksum env vars in the ObjectStore before going further.

---

## Phase 4 — Scale `1 → 2 → 3`

Do this **before** the cutover window so `-9` is already an HA cluster when it goes
live. Scaling an empty cluster is fast and cheap.

**ACTION** (one step at a time; validate between each):

```bash
kubectl -n totallylegitco patch cluster fhi-pg-main-9 --type=merge -p '{"spec":{"instances":2}}'
kubectl -n totallylegitco get cluster fhi-pg-main-9 -w      # wait 2/2 ready
kubectl -n totallylegitco patch cluster fhi-pg-main-9 --type=merge -p '{"spec":{"instances":3}}'
kubectl -n totallylegitco get cluster fhi-pg-main-9 -w      # wait 3/3 ready
```

**VALIDATION**

```bash
# each -9 replica: has the sidecar, HA slot active, bounded replay lag
kubectl -n totallylegitco exec fhi-pg-main-9-1 -c postgres -- psql -U postgres -xc \
 "SELECT application_name, state, replay_lag FROM pg_stat_replication;"
# (optional) reuse the checker for sidecar + lag; ignore its -8-streaming section:
./scripts/check-pg9-replication.sh || true
```

**GATE (each step):** the new replica reaches ready, its `plugin-barman-cloud`
sidecar is present, its HA slot is active, and replay lag is bounded. Do not add the
next instance until the current one is green.

---

## ⏱️ Cutover window begins here (Phases 5–9 — app is down)

Everything above was a no-op against production. From here the app is in
maintenance until Phase 9 completes. **Downtime ≈ dump + restore duration.** You are
doing this one-shot (no prior rehearsal), so move deliberately and validate each gate.

## Phase 5 — Cutover window: quiesce `-8` writes

**ACTION**

```bash
# Put the app into maintenance / scale write-producing workloads to 0 so nothing
# writes to -8 while we dump it. (cutover-app-to-pg9.sh in Phase 9 also does this,
# but for a clean dump you must stop writes BEFORE Phase 6.)
kubectl -n totallylegitco scale deployment/web --replicas=0
# stop any other writers (ray workers, beat, etc.) that write to the DB:
kubectl -n totallylegitco delete raycluster raycluster-kuberay || true
```

**VALIDATION**

```bash
kubectl -n totallylegitco exec fhi-pg-main-8-3 -c postgres -- psql -U postgres -Atc \
 "SELECT count(*) FROM pg_stat_activity WHERE usename='ziggystardust';"   # want 0
```

**GATE:** `-8` shows **0** active `ziggystardust` (app) backends. If non-zero, find
and stop the remaining writer before dumping — a dump taken while the app writes
will miss the delta.

---

## Phase 6 — Dump `-8`

**ACTION**

```bash
DEST_DIR=/secure/backups/pg8-cutover ./scripts/backup-pg8-live-base.sh
```

**VALIDATION / GATE:** the script self-gates — it confirms the `app` custom-format
dump is non-trivial in size, that `pg_restore --list` yields a readable TOC, and
that the globals dump is non-empty. Keep the files; they are also your off-cluster
safety copy. Do not proceed without a validated dump.

---

## Phase 7 — Restore into `-9`

**ACTION**

```bash
# points at the newest app-*.dump / globals-*.sql in DUMP_DIR, restores into the
# empty -9 primary. Refuses if -9 already has user tables (safety).
DUMP_DIR=/secure/backups/pg8-cutover ./scripts/restore-pg9-from-dump.sh
```

**VALIDATION / GATE:** the script self-gates — it confirms `-9` is a writable
primary, restores globals (tolerating the expected "role already exists" for
`postgres`/`ziggystardust`), `pg_restore`s the `app` DB, then verifies user tables
exist, `django_migrations` is populated, and tables are owned by `ziggystardust`.
Investigate any `[FAIL]` before continuing.

---

## Phase 8 — Data validation (`-8` vs `-9`)

**ACTION** (writes are stopped, so counts must match **exactly** now):

```bash
CRITICAL_TABLES="django_migrations auth_user <add-your-critical-tables>" \
  ./scripts/validate-pg8-vs-pg9.sh
```

**VALIDATION / GATE:** `DATA VALIDATION PASSED` — db list, roles, extensions,
**exact** `COUNT(*)` for critical tables, migration history, and app-role presence
all match between `-8` and `-9`. Because `-8` writes are quiesced, a count mismatch
is now a **hard FAIL** (not the transient drift it would be with a live source).
**Caveat:** a name in `CRITICAL_TABLES` that does not exist on `-8` is reported
WARN and skipped — list only tables you know exist so a typo can't hide a gap.

---

## Phase 9 — Flip `PDBHOST` → `-9` (cutover the app)

`cutover-app-to-pg9.sh`: asserts `-9` is a writable primary (trivially true for a
fresh primary); confirms `-8` has no active client backends; **saves the reverse
patch to disk**; patches `fhi-db-config/PDBHOST` →
`fhi-pg-main-9-rw.totallylegitco.svc`; brings the app back and restarts Ray; then
smoke-checks that app backends appear on `-9` and NOT on `-8`.

**ACTION**

```bash
./scripts/cutover-app-to-pg9.sh      # flips PDBHOST -> -9 and rolls the app
```

**VALIDATION**

```bash
kubectl -n totallylegitco get configmap fhi-db-config -o jsonpath='{.data.PDBHOST}{"\n"}'
kubectl -n totallylegitco rollout status deployment/web --timeout=300s
# confirm NO app backends remain on -8:
kubectl -n totallylegitco exec fhi-pg-main-8-3 -c postgres -- psql -U postgres -Atc \
 "SELECT count(*) FROM pg_stat_activity WHERE usename='ziggystardust';"   # want 0
```

**VALIDATION / GATE:** app health green against `-9`; write a canary row and read it
back; error rates normal; zero app backends left on `-8`. **The cutover window ends
here.** Keep `-8` intact and clientless as the rollback target — do NOT decommission
`-8` yet.

> **Make the flip durable (GitOps).** `cutover-app-to-pg9.sh` patched the *live*
> ConfigMap. The committed source of truth is `k8s/db-config.yaml`, and
> `scripts/build.sh` applies it before rolling the prod workloads. So set
> `PDBHOST: "fhi-pg-main-9-rw.totallylegitco.svc"` in `k8s/db-config.yaml` and
> commit it. **If you skip the commit, the next `build.sh` re-applies the old `-8`
> value and silently reverts the cutover** (build.sh prints the resolved `PDBHOST`
> so you can catch this).

---

## Phase 10 — Rollback semantics

Choose by whether the app has written to `-9` since cutover:

- **Pre-write** (no writes hit `-9` yet — trivial): point the app back at `-8`.
  ```bash
  ./scripts/rollback-pre-write-cutover.sh
  ```
  `-8` was only made clientless, never changed; this is the safe, fast path (the
  script DETECTS likely writes on `-9` and REFUSES if any are found). Because the
  seed was a logical dump and `-8` is untouched, a pre-write rollback loses nothing.
- **Post-write** (writes landed on `-9`): `-8` is now stale. Options, in order:
  1. **Re-dump forward:** quiesce `-9`, dump `-9`, restore into a fresh `-8`-side
     cluster, and cut back with the same quiesce→dump→restore→flip gate.
  2. **Restore from `-9`'s ObjectStore** (`fhi-backup-store-9`) into a recovery
     cluster and cut to it.
  3. **Accept loss:** last resort — reconcile the delta manually and record exactly
     what was lost.

**GATE:** exactly one writable primary throughout any rollback; never re-open writes
on `-8` while `-9` is also writable.

---

## Phase 11 — Backup + WAL-bounded verification (do this before trusting `-9`)

**Why last, and why mandatory:** cutover made `-9` the live database. Until its own
backup path is proven end-to-end **and** you have proven WAL cannot grow unbounded,
you are running production on the same silent-failure setup that caused the `-8`
incident. This phase is the green light for
[Decommission `-8`](#decommission--8-only-after-a-verified--9-restore). Each step is
`ACTION → VALIDATION → GATE`.

### 11a. A fresh post-load backup completes

Phase 3 proved backups worked while `-9` was empty; now prove it with real data.

**ACTION**
```bash
kubectl -n totallylegitco cnpg backup fhi-pg-main-9
kubectl -n totallylegitco get backup -l cnpg.io/cluster=fhi-pg-main-9 -w
```

**VALIDATION**
```bash
kubectl -n totallylegitco get backup -l cnpg.io/cluster=fhi-pg-main-9 \
  -o custom-columns=NAME:.metadata.name,PHASE:.status.phase,STOPPED:.status.stoppedAt
aws --endpoint-url https://s3.us-west-004.backblazeb2.com s3 ls \
  s3://fhi-pg-backup-second/fhi-pg-main-9/base/ | tail
```

**GATE:** newest `fhi-pg-main-9` Backup is `phase=completed` with a non-empty
`stoppedAt`, and a base backup object exists under `.../fhi-pg-main-9/` (nothing of
`-9`'s under `fhi-pg-main-8/`).

### 11b. WAL archiving is alive (so WAL drains, not grows)

**ACTION**
```bash
kubectl -n totallylegitco exec fhi-pg-main-9-1 -c postgres -- psql -U postgres -c 'SELECT pg_switch_wal();'
sleep 20
kubectl -n totallylegitco exec fhi-pg-main-9-1 -c postgres -- psql -U postgres -xc \
 "SELECT archived_count, failed_count, last_archived_time, last_failed_wal FROM pg_stat_archiver;"
```

**GATE:** after the forced switch, `archived_count` **increased**,
`last_archived_time` is recent, and `failed_count` is **flat**. A live archiver is
what keeps WAL draining to the object store instead of piling up in `pg_wal`.

### 11c. WAL is bounded and cannot grow beyond the cap

This is the direct antidote to the `-8-1` disk-full failure. Prove the ceiling is
set and current WAL is well under it.

**ACTION / VALIDATION**
```bash
# the slot-retention ceiling is set (manifest sets 16GB):
kubectl -n totallylegitco exec fhi-pg-main-9-1 -c postgres -- psql -U postgres -Atc \
 "SHOW max_slot_wal_keep_size;"                                  # want 16GB (not -1 / unset)
# no slot is retaining WAL near the ceiling:
kubectl -n totallylegitco exec fhi-pg-main-9-1 -c postgres -- psql -U postgres -c \
 "SELECT slot_name, active,
         pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS retained
    FROM pg_replication_slots ORDER BY retained DESC;"
# current pg_wal footprint (segments * 16MB). Should be a few GB, nowhere near 20:
kubectl -n totallylegitco exec fhi-pg-main-9-1 -c postgres -- \
  bash -c 'ls -1 /var/lib/postgresql/data/pgdata/pg_wal/*.ready 2>/dev/null | wc -l; du -sh /var/lib/postgresql/data/pgdata/pg_wal'
```

**GATE:** `max_slot_wal_keep_size` is `16GB` (a hard ceiling — a stuck slot is
invalidated rather than allowed to fill the disk), no slot's retained WAL is
approaching that ceiling, and `pg_wal` total is a small multiple of GB (well under
20). If any slot's `retained` is climbing toward 16GB, treat it as the `-8` pattern:
find the inactive consumer and drop the slot.

### 11d. The WAL-growth alert is live (fires above 20GB)

**ACTION / VALIDATION**
```bash
# the PrometheusRule is installed and the PodMonitor is scraping -9:
kubectl -n totallylegitco get prometheusrule fhi-pg-main-9-backup-wal
kubectl -n totallylegitco get podmonitor -l cnpg.io/cluster=fhi-pg-main-9
# confirm the alert's metric actually exists on -9 (VERIFY-LIVE the exact name):
kubectl -n totallylegitco exec fhi-pg-main-9-1 -c postgres -- \
  curl -s localhost:9187/metrics | grep -E 'cnpg_pg_wal_files|cnpg_pg_stat_archiver_failed|last_available_backup'
```

**GATE:** `PrometheusRule/fhi-pg-main-9-backup-wal` exists, the PodMonitor is
scraping, and the metrics the rules reference resolve in your Prometheus. The
**`FhiPg9WalGrowingBeyond20GB`** alert (`cnpg_pg_wal_files > 1280` ≈ 20GiB at 16MB
segments) is wired to paging, alongside `FhiPg9ArchiverFailing` and
`FhiPg9BackupTooOld`. **If the metric name differs on your build, fix the `expr` in
`k8s/fhi-pg-main-9-alerts.yaml` — keep the 20GB intent.** A cluster with no WAL/backup
alerting does **not** pass this phase; that blind spot is exactly what hid the `-8`
failure for months.

### 11e. Restore drill — the only real proof of a backup

A backup you have never restored is a hypothesis. Restore `-9`'s ObjectStore into a
**throwaway** cluster and sanity-check it.

**ACTION** (recovery into a disposable cluster; never touch `-9`):
```bash
# Adapt the root-level pg-recover.yaml template into a NEW cluster (e.g.
# fhi-pg-restore-drill) that recovers from -9's PLUGIN ObjectStore:
#   - recovery source -> externalCluster whose plugin references
#     barmanObjectName: fhi-backup-store-9 (serverName fhi-pg-main-9)
#   - creds secret: pg-backup2 ; distinct metadata.name + PVC (never reuse -9's serverName)
$EDITOR pg-recover.yaml    # produce restore-drill.yaml per the notes above
kubectl apply -f restore-drill.yaml
kubectl -n totallylegitco get cluster fhi-pg-restore-drill -w
```

**VALIDATION**
```bash
# row counts on the restored cluster match -9 for the critical tables. The
# validator derives the target pod from DST_CLUSTER's label, so pass DST_CLUSTER.
CRITICAL_TABLES="django_migrations auth_user <add-your-critical-tables>" \
  SRC_POD=fhi-pg-main-9-1 DST_CLUSTER=fhi-pg-restore-drill \
  ./scripts/validate-pg8-vs-pg9.sh
kubectl -n totallylegitco logs fhi-pg-restore-drill-1 -c postgres | grep -i 'recovery\|consistent'
```

**GATE:** the drill cluster reaches a consistent recovery point and its
critical-table counts match `-9`. **This is the gate that unlocks decommission.**
Tear the drill down afterward (`kubectl delete cluster fhi-pg-restore-drill` + its
PVC) so it does not itself accrue backups.

---

## Decommission `-8` (only after a verified `-9` restore)

Do **not** delete `-8` until **all of [Phase 11](#phase-11--backup--wal-bounded-verification-do-this-before-trusting--9)
is green** — in particular the **11e restore drill**. Until then `-8` is the
rollback of last resort. When you do decommission, remember `-8`'s backups live under
the separate `fhi-pg-main-8/` prefix; retiring them is a deliberate, separate step.
