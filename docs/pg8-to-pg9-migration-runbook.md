# Runbook — migrate `fhi-pg-main-8` → `fhi-pg-main-9` (CloudNativePG)

**Single, master, gated runbook** for standing up **`fhi-pg-main-9`** as a
continuously streaming **replica** of the live **`fhi-pg-main-8`**, verifying it
end-to-end, then cutting the application over — while never interrupting the
only healthy production primary (`fhi-pg-main-8-3`).

This document consolidates everything: the provisioning side (ObjectStore,
replica Cluster, source prep, streaming/backup verification) **and** the cutover
side (app DB-host parameterization, promotion, app switch, rollback). All
supporting artifacts live in this same branch — see
[Deliverables](#deliverables-all-in-this-branch).

> **Every step is `ACTION → VALIDATION → GATE`. Do NOT proceed past a step until
> its own validation is green.** Commands are copy-pasteable. Destructive /
> promotion steps require explicit confirmation and are called out.

Grounding evidence: `notes/incident-evidence.md`,
`notes/plan-01-fhi-pg-9-and-backups.md`. Deep dives:
[`docs/pg-reliability-hardening.md`](./pg-reliability-hardening.md) (why `-8`
broke + how the plugin was repaired) and
[`k8s/db-host-configurability.md`](../k8s/db-host-configurability.md) (how the
app DB host became one reversible switch).

---

## At-a-glance — the whole migration in order

Run top to bottom. Each phase has its own gate; a red gate stops the line.

| # | Phase | One-line goal | Green gate |
|---|---|---|---|
| 0 | [Fix `-8` archiving](#phase-0--fix--8-archiving--drain-stuck-wal-live-human-run) | Unwedge WAL archiving + shrink pgdata on `-8` | fresh `-8` backup `completed`; archiver advancing |
| 1 | [Prereqs on `-8`](#phase-1--migration-prerequisites-on--8-role--pg_hba--wal-retention) | Migration role + `pg_hba` replication + WAL retention | `check-pg8-source.sh` → `SOURCE PREFLIGHT PASSED` |
| 2 | [Parameterize app DB host](#phase-2--parameterize-the-app-db-host-safe-no-op-now-the-cutover-switch-later) | Apply `fhi-db-config` (no-op today) so cutover is one flip | app still talks to `-8`; ConfigMap = current host |
| 3 | [Safety-net dump](#phase-3--safety-net-logical-dump-of--8) | Off-cluster logical dump of `-8` | validated dump in hand off-cluster |
| 4 | [Apply ObjectStore + Cluster](#phase-4--apply-the-objectstore-then-the-cluster) | Create `-9` ObjectStore then replica Cluster | `-9-1` Running, in recovery (replica) |
| 5 | [Verify streaming](#phase-5--verify-streaming-no-slot-expected) | Prove `-9` streams from `-8` | `check-pg9-replication.sh` → `-9 REPLICATION HEALTHY` |
| 6 | [Verify `-9` backups](#phase-6--verify--9-archiving--a-fresh--9-backup) | `-9`'s OWN archiver + first backup work | `-9` Backup `completed`; objects in bucket |
| 7 | [Scale 1→2→3](#phase-7--scale-123-with-per-step-validation) | Add healthy `-9` replicas one at a time | each new replica ready + sidecar + bounded lag |
| 8 | [Data validation](#phase-8--data-validation) | Compare `-8` vs `-9` data | `validate-pg8-vs-pg9.sh` → `DATA VALIDATION PASSED` |
| 9 | [Quiesce, zero-lag, promote](#phase-9--quiesce-zero-lag-promote) | Stop `-8` writes, promote `-9` | `pg_is_in_recovery()=f` on `-9`; `-8` fenced |
| 10 | [App cutover](#phase-10--app-cutover-keep--8-as-rollback) | Flip `PDBHOST` to `-9`, roll the app | app healthy on `-9`; `-8` kept as rollback |
| 11 | [Rollback semantics](#phase-11--rollback-semantics) | Know how to reverse, pre- and post-write | exactly one writable primary always |
| — | [Decommission `-8`](#decommission--8-only-after-a-verified--9-restore) | Only after a verified `-9` restore drill | `-9` restore drill green |

---

## Hard environment facts

| Thing | Value |
|---|---|
| Namespace | `totallylegitco` |
| Source cluster | `fhi-pg-main-8` (only healthy primary: `fhi-pg-main-8-3`) |
| Source rw service | `fhi-pg-main-8-rw.totallylegitco.svc` |
| Target cluster | `fhi-pg-main-9` (rw service `fhi-pg-main-9-rw.totallylegitco.svc`) |
| PG image | `ghcr.io/cloudnative-pg/postgresql:18.1-system-trixie` |
| Operator | CloudNativePG `v1.28.0` |
| Backup plugin | barman-cloud `v0.13` (sidecar image `…/plugin-barman-cloud:v0.13.0`) |
| Storage class | `encrypted-local-path` (RWO) |
| App DB / owner | `app` / `ziggystardust` |
| Migration role | `fhi_pg9_migration` (LOGIN REPLICATION) |
| Migration secret | `fhi-pg-main-9-source` (key `password`) |
| App secret | `fhi-internal-pg-secret` |
| Superuser secret | `fhi-superuser-pg-secret` |
| Backup creds | `pg-backup2` (`PG_ACCESS_KEY_ID` / `PG_ACCESS_SECRET_KEY`) |
| Bucket / endpoint | `s3://fhi-pg-backup-second/` @ `https://s3.us-west-004.backblazeb2.com` |
| -9 ObjectStore | `fhi-backup-store-9`, serverName `fhi-pg-main-9` |
| App DB-host switch | ConfigMap `fhi-db-config`, key `PDBHOST` (the single cutover flip) |

## Deliverables (all in this branch)

Manifests:

- `k8s/fhi-pg-main-9-objectstore.yaml` — `-9`'s isolated Barman ObjectStore.
- `k8s/fhi-pg-main-9-cluster.yaml` — the `-9` replica Cluster.
- `k8s/db-config.yaml` — `fhi-db-config` ConfigMap holding `PDBHOST` (the cutover switch).
- `k8s/deploy.yaml`, `k8s/ray/cluster.yaml` — prod workloads wired to `PDBHOST` from that ConfigMap.

Scripts (all `set -euo pipefail`, context-checked, namespace-defaulted, password-safe, idempotent where practical):

- `scripts/check-pg8-source.sh` — read-only source preflight.
- `scripts/backup-pg8-live-base.sh` — safety-net logical dump of `-8`.
- `scripts/create-or-fix-pg9-migration-role.sh` — create/repair the migration role.
- `scripts/check-pg9-replication.sh` — verify `-9` streaming + sidecar + archiver.
- `scripts/validate-pg8-vs-pg9.sh` — data consistency `-8` vs `-9`.
- `scripts/promote-pg9.sh` — promote `-9` (self-gates on **quiesced `-8` writes** (zero active client backends) + **zero replay lag**; it does NOT itself fence `-8` — you must scale writers to 0 first).
- `scripts/cutover-app-to-pg9.sh` — flip `PDBHOST` → `-9`, roll the app.
- `scripts/rollback-pre-write-cutover.sh` — pre-write rollback of the cutover.

Docs:

- `docs/pg8-to-pg9-migration-runbook.md` — **this file**.
- `docs/pg-reliability-hardening.md` — root cause + prevention.
- `k8s/db-host-configurability.md` — the DB-host refactor rationale + reversibility.

---

## ⚠️ WHAT NOT TO DO (read first, honored by every script)

- **Never** `kubectl delete pod fhi-pg-main-8-3` or delete its PVC — it is the
  ONLY healthy primary; losing it loses the database.
- **Do not assume `fhi-pg-main-8-2` is healthy.** Treat it as suspect; do all
  source reads/dumps from `-8-3`.
- **Never reuse `serverName: fhi-pg-main-8`** for `-9`'s ObjectStore/backups —
  it would corrupt/overwrite `-8`'s backup lineage. `-9` uses `fhi-pg-main-9`.
- **Do not rely on `-8`'s object-store WAL archive for the migration.** It has
  been failing for months (`archived_count=0`, `failed_count≈247k`). `-9` seeds
  by **streaming directly** from `-8`, not by restoring from the archive.
- **Never promote `-9` before replay lag is zero** and writes are quiesced.
- **Never allow writes to BOTH clusters** at once (split brain). Exactly one
  writable primary at all times.
- **Do not manually create/delete CNPG-managed pods** (`fhi-pg-main-9-*`). Let
  the operator own them; scale via `spec.instances`.
- **Do not uninstall the old barman-cloud Helm release** without first
  transferring ownership of shared resources (the `barman-cloud` Service, certs)
  — a blind uninstall re-breaks archiving (see the hardening doc).
- **Never** put a generated password in a committed manifest, a log line, or a
  command-line argument.

---

## Phase 0 — Fix `-8` archiving + drain stuck WAL (LIVE, human-run)

**Why first:** `-8`'s pgdata is bloated (~229GB, mostly *unarchivable* WAL) and
its archiver is wedged on a missing `00000007.history` file. We must clear the
wedge and shrink pgdata **before** cloning, or `-9` inherits the bloat and the
clone takes far longer / may not fit 50Gi. (Full P0 plugin hotfix — Service
selector + leader lease — is in `notes/incident-evidence.md` and the hardening
doc; do that FIRST if the plugin itself is still unreachable.)

> **HARD GATE — this whole phase must be GREEN before the clone (Phase 4).** The
> clone's WAL-retention safety net is `-8`'s `restore_command` reading `-8`'s
> **object-store archive** (see cluster manifest externalCluster `plugin`).
> That fallback is worthless if the archive is dead. Therefore "archive verified
> healthy on -8" is a **prerequisite for cloning**, not a nice-to-have.

> **DO NOT touch `fhi-pg-main-8-3`.** Everything below is achievable WITHOUT
> deleting/restarting/​recreating the only healthy primary. Never `delete pod
> fhi-pg-main-8-3` and never delete its PVC. If — and only if — fixing archiving
> genuinely required restarting `-3`, that is a **separate live-incident
> escalation**: first stand up and verify a healthy replacement primary
> (promote a caught-up replica), THEN handle `-3`. That escalation is out of
> scope for this runbook and must never be green-lit here.

**ACTION** — fix archiving using non-primary-disruptive steps only:

```bash
# inspect which plugin image each pod actually runs (skew is the root cause)
for pod in fhi-pg-main-8-1 fhi-pg-main-8-2 fhi-pg-main-8-3; do
  kubectl -n totallylegitco get pod "$pod" \
    -o jsonpath='{.metadata.name}: {range .spec.initContainers[*]}{.name}={.image} {end}{"\n"}' 2>/dev/null
done

# (a) drop STALE/inactive replication slots that retain WAL for dead -8 replicas
#     (these both bloat pgdata and can mask a healthy stream in later checks):
kubectl -n totallylegitco exec fhi-pg-main-8-3 -c postgres -- psql -U postgres -c \
 "SELECT slot_name, active, pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(),restart_lsn)) retained
    FROM pg_replication_slots WHERE NOT active ORDER BY retained DESC;"
# review the list, then drop only confirmed-stale ones:
#   SELECT pg_drop_replication_slot('<stale_slot_name>');

# (b) clear the wedged archive queue WITHOUT touching -3: recreate only a
#     REPLICA pod so CNPG rebuilds its pg_wal/archive_status (PVC preserved).
kubectl -n totallylegitco delete pod fhi-pg-main-8-1     # a REPLICA (never -8-3)
kubectl -n totallylegitco logs fhi-pg-main-8-1 -c plugin-barman-cloud -f
```

If a specific orphaned `*.history` / `*.partial` marker is still wedged, clear
only its stuck `archive_status/*.ready` marker per CNPG guidance (targeted, on a
**replica** pod — never on `-3`), then force WAL to advance from the primary
(a plain SQL call, no restart):

```bash
kubectl -n totallylegitco exec fhi-pg-main-8-3 -c postgres -- \
  psql -U postgres -c 'SELECT pg_switch_wal();'   # SQL only; does NOT restart -3
sleep 15
```

**VALIDATION**

```bash
kubectl -n totallylegitco exec fhi-pg-main-8-3 -c postgres -- psql -U postgres -xc \
 "SELECT archived_count, failed_count, last_archived_wal, last_archived_time, last_failed_wal
    FROM pg_stat_archiver;"
# then confirm a FRESH base backup of -8 actually completes (proves end-to-end):
kubectl -n totallylegitco cnpg backup fhi-pg-main-8
kubectl -n totallylegitco get backup -l cnpg.io/cluster=fhi-pg-main-8
```

**GATE (HARD):** `archived_count` **increased** after the forced switch,
`last_archived_time` is recent, `failed_count` is **flat**, pgdata usage is
trending **down**, **and** a fresh `-8` base backup reaches `phase=completed`.
If any of these is red → **stop**; fix the plugin/CA per the hardening doc. Do
not clone `-9` off a source whose archive cannot serve `restore_command`.

---

## Phase 1 — Migration prerequisites on `-8` (role + pg_hba + WAL retention)

### 1a. Create/repair the migration role

**ACTION**

```bash
# uses the password already in secret fhi-pg-main-9-source (never invents one)
./scripts/create-or-fix-pg9-migration-role.sh
```

**VALIDATION / GATE:** the script self-gates — it prints `[PASS]` only when
`fhi_pg9_migration` has `rolcanlogin=t rolreplication=t` **and** both auth probes
(normal + physical replication) succeed against `fhi-pg-main-8-rw`. Do not
proceed on any `[FAIL]`.

### 1b. `-8` pg_hba must allow the REPLICATION connection

`host all all all md5` on `-8` does **not** cover physical replication — those
connections match only pg_hba lines whose database column is the special keyword
`replication`. If probe B in 1a reported `AUTH REJECTED`, add an entry to
`fhi-pg-main-8`'s **CNPG-managed** `spec.postgresql.pg_hba` and let it reconcile:

```yaml
# in the fhi-pg-main-8 Cluster spec (managed elsewhere / colo-scripts):
spec:
  postgresql:
    pg_hba:
      - host replication fhi_pg9_migration all md5   # add; keep existing lines
```

```bash
# CNPG Cluster is a CRD; `kubectl rollout status` does NOT understand it.
# Wait on the CNPG Ready condition (or use the cnpg plugin status):
kubectl -n totallylegitco wait --for=condition=Ready --timeout=300s cluster/fhi-pg-main-8
# alternative: kubectl cnpg status fhi-pg-main-8 -n totallylegitco
./scripts/create-or-fix-pg9-migration-role.sh                    # re-run; probe B must PASS
```

**GATE:** probe B (physical replication) PASSES.

### 1c. WAL retention on `-8` for the clone window (NO no-op slot)

**Resolved design (not a VERIFY-LIVE guess).** A named physical slot on `-8`
**cannot** protect this clone: CNPG 1.28 treats `primary_slot_name` /
`primary_conninfo` as **fixed, user-unsettable** parameters
(`pkg/postgres/configuration.go`), the `externalClusters` schema has no slot
field, and the replica-cluster docs stream **slotless**. A hand-made slot would
retain WAL that `-9` never consumes → disk pressure on the only healthy primary
with **zero** protection, then invalidation at `max_slot_wal_keep_size`. **Do not
create one.**

The retention guarantee is therefore **two real mechanisms**:

1. **`restore_command` fallback from `-8`'s repaired archive** (the actual
   guarantee) — wired in the manifest's externalCluster `plugin` stanza and
   gated by Phase 0. If a segment is recycled on `-8` before `-9` streams it,
   `-9`'s designated primary fetches it from the object store.
2. **A COMPUTED `wal_keep_size` on `-8`** (the belt that minimizes how often #1
   is exercised) — sized from observed WAL rate, **not** a fixed default.

**ACTION** — compute `wal_keep_size` from live data, validate against free disk,
then set it (and bound slot retention so `-8` can never fill again):

```bash
# (1) observe peak WAL generation rate on -8 over a representative window.
#     Sample current_wal_lsn twice, spaced by WINDOW seconds:
WINDOW=300
L1=$(kubectl -n totallylegitco exec fhi-pg-main-8-3 -c postgres -- psql -U postgres -Atc "SELECT pg_current_wal_lsn();")
sleep "$WINDOW"
L2=$(kubectl -n totallylegitco exec fhi-pg-main-8-3 -c postgres -- psql -U postgres -Atc "SELECT pg_current_wal_lsn();")
BYTES=$(kubectl -n totallylegitco exec fhi-pg-main-8-3 -c postgres -- psql -U postgres -Atc "SELECT pg_wal_lsn_diff('$L2','$L1');")
RATE_MBs=$(python3 -c "print(($BYTES)/$WINDOW/1048576)")
echo "observed WAL rate: ${RATE_MBs} MB/s  (sample this at PEAK load, not idle)"

# (2) size wal_keep_size = peak_rate * worst_case_clone_seconds * safety_margin.
#     Estimate clone seconds from pgdata size / expected basebackup throughput,
#     rounded UP, then apply a large margin (>=3x) for load spikes + retries:
CLONE_SECONDS=${CLONE_SECONDS:-7200}   # worst-case; MEASURE, do not guess low
MARGIN=${MARGIN:-3}
KEEP_MB=$(python3 -c "import math;print(int(math.ceil($RATE_MBs*$CLONE_SECONDS*$MARGIN)))")
echo "recommended wal_keep_size >= ${KEEP_MB} MB"

# (3) VALIDATE it fits: KEEP_MB must be well under -8's FREE disk on the pgdata PVC.
kubectl -n totallylegitco exec fhi-pg-main-8-3 -c postgres -- df -Pm /var/lib/postgresql/data
# CONFIRM: free MB on that filesystem >> KEEP_MB (leave headroom for normal WAL +
# the archive backlog). If it does NOT fit, you MUST rely on mechanism #1
# (archive restore_command) and keep wal_keep_size modest -- do NOT set a
# wal_keep_size that could itself fill -8. Record which mechanism is in force.

# (4) apply (prefer -8's CNPG spec.postgresql.parameters so it survives a pod
#     recreate; ALTER SYSTEM shown as the fast path for the window):
kubectl -n totallylegitco exec fhi-pg-main-8-3 -c postgres -- \
  psql -U postgres -c "ALTER SYSTEM SET wal_keep_size = '${KEEP_MB}MB';"
# bound slot retention so NO slot can ever fill -8's disk (the -8-1 failure mode):
kubectl -n totallylegitco exec fhi-pg-main-8-3 -c postgres -- \
  psql -U postgres -c "ALTER SYSTEM SET max_slot_wal_keep_size = '80GB';"  # < free disk
kubectl -n totallylegitco exec fhi-pg-main-8-3 -c postgres -- \
  psql -U postgres -c "SELECT pg_reload_conf();"
```

**VALIDATION**

```bash
./scripts/check-pg8-source.sh
```

**GATE:** `check-pg8-source.sh` prints `SOURCE PREFLIGHT PASSED` — it now FAILS
(not warns) if `max_slot_wal_keep_size` is unbounded/unset, if there is no FREE
`wal_sender`/`replication_slot` headroom, or if any stale/inactive slot retains
WAL beyond threshold. Confirm the computed `wal_keep_size` from step (2) is
applied and fits free disk from step (3). **State explicitly in your change log
which retention mechanism is primary** (archive restore_command vs wal_keep_size)
based on whether the computed size fit the disk.

---

## Phase 2 — Parameterize the app DB host (safe no-op now, the cutover switch later)

**Why now:** the app's Postgres host is read from the `PDBHOST` env var. Before
this refactor it came ONLY from opaque, un-versioned cluster Secrets, so the
cutover would have meant hand-editing a live Secret. Instead we introduce one
version-controlled switch — the `fhi-db-config` ConfigMap — and wire every
DB-consuming prod workload to read `PDBHOST` from it via an explicit container
`env` entry (which takes precedence over the `envFrom` Secret value for the same
key). **The ConfigMap defaults to the current `-8` host, so applying this now
changes nothing at runtime** — it just pre-positions the single flip that
Phase 10 will use. Full rationale + the "deliberately not changed" list:
[`k8s/db-host-configurability.md`](../k8s/db-host-configurability.md).

Applying this early (rather than at cutover) de-risks Phase 10: the ConfigMap
and env wiring are proven live and harmless well before they matter.

**ACTION**

```bash
# 1) create the switch (defaults PDBHOST = fhi-pg-main-8-rw.totallylegitco.svc)
kubectl apply -f k8s/db-config.yaml

# 2) roll out the workloads that now read PDBHOST from the ConfigMap.
#    (Substitute your deploy pipeline's env expansion for ${FHI_BASE}/${FHI_VERSION};
#     these manifests use envsubst-style placeholders.)
kubectl apply -f k8s/deploy.yaml
kubectl apply -f k8s/ray/cluster.yaml   # if the Ray workers are managed this way
```

**VALIDATION** — confirm it was a true no-op (still pointing at `-8`):

```bash
# ConfigMap holds the CURRENT -8 host
kubectl -n totallylegitco get configmap fhi-db-config -o jsonpath='{.data.PDBHOST}{"\n"}'
# a fresh web pod actually resolved PDBHOST to -8
kubectl -n totallylegitco get pods -l app=fight-health-insurance-prod -o name | head -1 | \
  xargs -I{} kubectl -n totallylegitco exec {} -- printenv PDBHOST
# the app is still healthy against -8
kubectl -n totallylegitco rollout status deployment/web --timeout=300s
```

**GATE:** `PDBHOST` in both the ConfigMap and a running web pod is
`fhi-pg-main-8-rw.totallylegitco.svc`, and `deployment/web` is healthy. Nothing
about production traffic changed — you have only installed the switch. **Do NOT
change `PDBHOST` yet**; the flip to `-9` happens only in Phase 10, after `-9` is
promoted.

---

## Phase 3 — Safety-net logical dump of `-8`

**Why:** independent of the streaming clone, so if the live-raw seed fails we can
still recover.

**ACTION**

```bash
DEST_DIR=/secure/backups/pg8-safety ./scripts/backup-pg8-live-base.sh
```

**VALIDATION / GATE:** the script self-gates — it confirms the `app` custom-
format dump is non-trivial in size, that `pg_restore --list` yields a readable
TOC, and that the globals dump is non-empty. Store the output **off-cluster**.
Do not proceed without a validated dump in hand.

---

## Phase 4 — Apply the ObjectStore, then the Cluster

**ACTION** (order matters — the ObjectStore must exist before the Cluster that
references it):

```bash
kubectl apply -f k8s/fhi-pg-main-9-objectstore.yaml
kubectl -n totallylegitco get objectstore fhi-backup-store-9   # exists

kubectl apply -f k8s/fhi-pg-main-9-cluster.yaml
```

**VALIDATION** — watch bootstrap (physical `pg_basebackup` from `-8`, then it
enters continuous replica streaming):

```bash
kubectl -n totallylegitco get cluster fhi-pg-main-9 -w
kubectl -n totallylegitco get pods -l cnpg.io/cluster=fhi-pg-main-9
kubectl -n totallylegitco logs -l cnpg.io/cluster=fhi-pg-main-9 -c postgres --tail=50
```

**GATE:** `fhi-pg-main-9-1` is `Running`, the Cluster reports the instance
healthy, and it is a **replica** (in recovery), not a fresh empty primary.

> **VERIFY-LIVE (managed roles / superuser on a read-only replica):** while `-9`
> is a replica, CNPG **cannot** run role/password DDL on it (read-only). The
> superuser and `ziggystardust` credentials on `-9` are the **physical copy from
> `-8`** until promotion. Ensure `fhi-superuser-pg-secret` and
> `fhi-internal-pg-secret` on `-9` match `-8`'s actual passwords, or expect them
> to be reconciled to the secret values **at promotion**. Confirm the operator's
> behaviour with `kubectl explain cluster.spec.managed.roles` and by watching the
> operator logs at promotion.

---

## Phase 5 — Verify streaming (no slot expected)

**ACTION / VALIDATION**

```bash
./scripts/check-pg9-replication.sh
```

**GATE:** `-9 REPLICATION HEALTHY` — the script proves `-9` is the connection
actually streaming by correlating `pg_stat_replication` on `-8` (matching `-9`'s
client address/`application_name`, `state=streaming`, and `sent_lsn`
**advancing** across two samples) rather than accepting "some physical slot is
active" (a stale `-8` HA slot could satisfy that). It also checks
`pg_is_in_recovery()=t` on `-9`, bounded replay lag, and the
`plugin-barman-cloud:v0.13` sidecar in **every** `-9` pod. **No slot is expected
on `-8`** (CNPG streams the replica cluster slotless — see Phase 1c); retention
is `wal_keep_size` + the archive `restore_command`. The archiver verdict is
reported **separately** and does not mask streaming health (see Phase 6 for the
real archiving gate).

---

## Phase 6 — Verify `-9` archiving + a fresh `-9` backup

**Why:** prove `-9`'s OWN backup path works before we depend on it. (`-9` must
never inherit `-8`'s dead archive.)

**ACTION**

```bash
# force WAL movement then confirm -9's archiver advances (see check script §6)
kubectl -n totallylegitco exec fhi-pg-main-9-1 -c postgres -- psql -U postgres -c 'SELECT pg_switch_wal();' || true
./scripts/check-pg9-replication.sh    # re-check archiver section

# take an explicit first backup of -9 and confirm it completes
kubectl -n totallylegitco cnpg backup fhi-pg-main-9
kubectl -n totallylegitco get backup -l cnpg.io/cluster=fhi-pg-main-9
```

**VALIDATION / GATE:** a `Backup` for `fhi-pg-main-9` reaches `phase=completed`,
and objects appear under `s3://fhi-pg-backup-second/fhi-pg-main-9/`. If `-9`'s
archiver shows `failed` **rising**, stop and check the B2 creds + the checksum
env vars in the ObjectStore.

> If `-9` (still a replica) does not archive its own WAL yet, that may be the
> `archive_mode = on` vs `always` behaviour flagged in the manifest — confirm
> with the operator; full `-9` archiving is guaranteed once `-9` is promoted.

---

## Phase 7 — Scale `1 → 2 → 3` with per-step validation

**ACTION** (one step at a time; validate between each):

```bash
kubectl -n totallylegitco patch cluster fhi-pg-main-9 --type=merge -p '{"spec":{"instances":2}}'
kubectl -n totallylegitco get cluster fhi-pg-main-9 -w      # wait 2/2 ready
./scripts/check-pg9-replication.sh                          # sidecar+lag on the new pod

kubectl -n totallylegitco patch cluster fhi-pg-main-9 --type=merge -p '{"spec":{"instances":3}}'
kubectl -n totallylegitco get cluster fhi-pg-main-9 -w      # wait 3/3 ready
./scripts/check-pg9-replication.sh
```

**GATE (each step):** the new replica reaches ready, its `plugin-barman-cloud`
sidecar is present, HA slots are active, and replay lag is bounded. Do not add
the next instance until the current one is green.

---

## Phase 8 — Data validation

**ACTION** (run when `-8` writes are quiet so counts are stable):

```bash
CRITICAL_TABLES="django_migrations auth_user <add-your-critical-tables>" \
  ./scripts/validate-pg8-vs-pg9.sh
```

**VALIDATION / GATE:** `DATA VALIDATION PASSED` — db list, roles, extensions,
**exact** `COUNT(*)` for critical tables, migration history, and app-role
presence all match between `-8` and `-9`. Investigate any mismatch before
cutover (transient sequence drift while `-8` still writes is reported as WARN,
not FAIL). **Caveat:** a name in `CRITICAL_TABLES` that does not exist on `-8` is
reported **WARN and skipped** (not FAIL) — double-check every table name so a
typo can't silently hide a real gap. List only tables you know exist on `-8`.

---

## Phase 9 — Quiesce, zero lag, promote

**Destructive / one-way-ish. Requires confirmation.**

**ACTION**

```bash
# 1) stop writes to -8 (app maintenance mode / scale app deploy to 0).
# 2) confirm zero replay lag on -9:
kubectl -n totallylegitco exec fhi-pg-main-9-1 -c postgres -- psql -U postgres -Atc \
 "SELECT pg_wal_lsn_diff(pg_last_wal_receive_lsn(), pg_last_wal_replay_lsn());"   # want 0
# 3) promote -9 (self-gates on: -8 has zero active client backends AND zero replay lag):
./scripts/promote-pg9.sh
```

**VALIDATION**

```bash
kubectl -n totallylegitco exec fhi-pg-main-9-1 -c postgres -- psql -U postgres -Atc \
 "SELECT pg_is_in_recovery();"    # must now be 'f' (promoted primary)
kubectl -n totallylegitco get cluster fhi-pg-main-9
```

**GATE:** `pg_is_in_recovery()=f` on the new `-9` primary; `-9` is healthy; and
`-8` has **no active client backends** (you quiesced its writers in step 1).
**Important:** nothing in this step makes `-8` technically read-only — it remains
a writable primary. Split-brain is prevented only by keeping every client
pointed away from `-8` (writers scaled to 0 now; `PDBHOST` still on `-8` until
Phase 10 flips it to `-9`). Keep `-8` clientless until decommission. **Never**
promote with non-zero lag or before `-8`'s writers are scaled to zero.

> `promote-pg9.sh` refuses to auto-run the DISTRIBUTED-topology token dance
> (it needs the `-8` manifest and cannot be validated offline) — for a
> standalone promotion it sets `.spec.replica.enabled: false`; for a distributed
> topology it prints the documented human procedure. Read its output.
>
> **Source-name check:** the script confirms `-9` really replicates from `-8`
> before promoting. The checked-in Cluster sets `spec.replica.source:
> fhi-pg-main-8-live` (the `externalClusters[].name`), which is intentionally
> distinct from the CNPG source cluster name `fhi-pg-main-8`. The script accepts
> this externalCluster name out of the box — if you renamed either, verify the
> source really is `-8` first, then re-run.

---

## Phase 10 — App cutover (keep `-8` as rollback)

This is where the Phase 2 switch finally flips. `cutover-app-to-pg9.sh`:
refuses unless `-9` promotion is verified; scales the write-producing workloads
to zero and confirms `-8` has no active client backends; **saves the reverse
patch to disk**; patches `fhi-db-config/PDBHOST` → `fhi-pg-main-9-rw.totallylegitco.svc` (the `-rw` service FQDN); brings the
app back and restarts Ray; then smoke-checks that app backends appear on `-9`
and NOT on `-8`.

**ACTION**

```bash
./scripts/cutover-app-to-pg9.sh      # flips PDBHOST -> -9 and rolls the app
```

**VALIDATION**

```bash
# the switch now points at -9
kubectl -n totallylegitco get configmap fhi-db-config -o jsonpath='{.data.PDBHOST}{"\n"}'
# app health + a canary write/read against -9
kubectl -n totallylegitco rollout status deployment/web --timeout=300s
# confirm NO app backends remain on -8
kubectl -n totallylegitco exec fhi-pg-main-8-3 -c postgres -- psql -U postgres -Atc \
 "SELECT count(*) FROM pg_stat_activity WHERE usename='ziggystardust';"   # want 0
```

**VALIDATION / GATE:** app health checks green against `-9`; write a canary row
and read it back; error rates normal; zero app backends left on `-8`. **Keep
`-8` intact and write-fenced** as the rollback target — do NOT decommission `-8`
yet.

---

## Phase 11 — Rollback semantics

Choose by whether the app has written to `-9` since cutover:

- **Pre-write** (no writes hit `-9` yet — trivial): point the app back at `-8`.
  ```bash
  ./scripts/rollback-pre-write-cutover.sh
  ```
  `-8` was only write-fenced, not changed; this is the safe, fast path. The
  script DETECTS likely writes on `-9` and REFUSES if any are found.
- **Post-write** (writes landed on `-9`): you cannot simply repoint — `-8` is now
  stale. Options, in order of preference:
  1. **Reverse replication:** stand `-8` (or a fresh cluster) up as a replica of
     `-9`, catch up, then fail back with the same quiesce→zero-lag→promote gate.
  2. **Restore:** restore from `-9`'s ObjectStore (`fhi-backup-store-9`) into a
     recovery cluster and cut to it.
  3. **Accept loss:** only as a last resort, replay the delta from `-9` onto `-8`
     and accept the documented data loss window. Record exactly what was lost.

**GATE:** exactly one writable primary throughout any rollback; never re-open
writes on `-8` while `-9` is also writable.

---

## Decommission `-8` (only after a verified `-9` restore)

Do **not** delete `-8` until `-9` has a **verified restore drill** green (restore
`fhi-backup-store-9` into a throwaway cluster and sanity-check row counts). Until
then `-8` is the rollback of last resort.

## Consolidated VERIFY-LIVE checklist (confirm with `kubectl explain`)

1. ~~External-primary physical slot binding~~ — **RESOLVED offline** against the
   CNPG 1.28 source/CRD/docs: not possible; replica clusters stream slotless.
   Retention is `wal_keep_size` + archive `restore_command` (Phase 0 + 1c). No
   live check needed.
2. Managed roles + superuser reconciliation timing on a read-only replica
   (`cluster.spec.managed.roles`, `cluster.spec.superuserSecret`). Phase 4.
3. `archive_mode on` vs `always` for a replica archiving its own WAL pre-
   promotion. Phase 6.
