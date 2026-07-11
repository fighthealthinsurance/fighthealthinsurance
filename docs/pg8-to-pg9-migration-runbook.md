# Runbook — migrate `fhi-pg-main-8` → `fhi-pg-main-9` (CloudNativePG)

Master, gated runbook for standing up **`fhi-pg-main-9`** as a continuously
streaming **replica** of the live **`fhi-pg-main-8`**, verifying it, then cutting
the app over. Grounded in `notes/incident-evidence.md` and
`notes/plan-01-fhi-pg-9-and-backups.md`.

> **Every step is `ACTION → VALIDATION → GATE`. Do not proceed past a step until
> its own validation is green.** Commands are copy-pasteable. Destructive/
> promotion steps require explicit confirmation and are called out.

## Hard environment facts

| Thing | Value |
|---|---|
| Namespace | `totallylegitco` |
| Source cluster | `fhi-pg-main-8` (only healthy primary: `fhi-pg-main-8-3`) |
| Source rw service | `fhi-pg-main-8-rw.totallylegitco.svc` |
| Target cluster | `fhi-pg-main-9` |
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

## Deliverables referenced by this runbook

Authored in **this** PR (`polly/pg9-provision`):

- `k8s/fhi-pg-main-9-objectstore.yaml`
- `k8s/fhi-pg-main-9-cluster.yaml`
- `scripts/check-pg8-source.sh`
- `scripts/backup-pg8-live-base.sh`
- `scripts/create-or-fix-pg9-migration-role.sh`
- `scripts/check-pg9-replication.sh`
- `scripts/validate-pg8-vs-pg9.sh`

Authored in the **sibling** PR (`polly/pg9-cutover`) — referenced here, run at
cutover:

- `scripts/promote-pg9.sh`
- `scripts/cutover-app-to-pg9.sh`
- `scripts/rollback-pre-write-cutover.sh`

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
  — a blind uninstall re-breaks archiving (see hardening doc).
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

**ACTION** — clear the archive wedge by rolling-recreating the **replica** pods
(replicas first, primary `-8-3` LAST — and only if it must be touched at all),
so CNPG rebuilds `pg_wal/archive_status` and re-derives the archive queue:

```bash
# inspect which plugin image each pod actually runs (skew is the root cause)
for pod in fhi-pg-main-8-1 fhi-pg-main-8-2 fhi-pg-main-8-3; do
  kubectl -n totallylegitco get pod "$pod" \
    -o jsonpath='{.metadata.name}: {range .spec.initContainers[*]}{.name}={.image} {end}{"\n"}' 2>/dev/null
done

# recreate a REPLICA first (never -8-3 here). PVC is preserved.
kubectl -n totallylegitco delete pod fhi-pg-main-8-1
kubectl -n totallylegitco logs fhi-pg-main-8-1 -c plugin-barman-cloud -f   # watch it come back
```

If a specific orphaned `*.history` / `*.partial` marker remains stuck, clear
only its `archive_status/*.ready` marker per CNPG guidance (targeted, on a
replica), then force WAL to advance:

```bash
# force a WAL switch so a fresh segment must archive
kubectl -n totallylegitco exec fhi-pg-main-8-3 -c postgres -- \
  psql -U postgres -c 'SELECT pg_switch_wal();'
```

**VALIDATION**

```bash
kubectl -n totallylegitco exec fhi-pg-main-8-3 -c postgres -- psql -U postgres -xc \
 "SELECT archived_count, failed_count, last_archived_wal, last_archived_time, last_failed_wal
    FROM pg_stat_archiver;"
```

**GATE:** `archived_count` is **increasing**, `last_archived_time` is recent, and
`failed_count` is **flat** (not climbing). pgdata usage is trending **down** as
archived WAL is recycled. If archiving is still failing → **stop**; fix the
plugin/CA per the hardening doc before going further.

---

## Phase 1 — Migration prerequisites on `-8` (role + pg_hba + slot)

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
kubectl -n totallylegitco rollout status cluster/fhi-pg-main-8   # wait for reconcile
./scripts/create-or-fix-pg9-migration-role.sh                    # re-run; probe B must PASS
```

**GATE:** probe B (physical replication) PASSES.

### 1c. Physical replication slot + retention guard on `-8`

**Why:** during the clone `-8` must **retain** the WAL `-9` still needs, but a
stuck slot must **never** be able to fill `-8`'s disk again (that is exactly what
killed `-8-1`).

> **VERIFY-LIVE (flagged in the cluster manifest):** CNPG 1.28's
> `externalClusters[]` has **no field** to bind `-9`'s streaming connection to a
> *named* physical slot on `-8`, and `replicationSlots.highAvailability` only
> manages `-9`'s **internal** slots. Confirm with
> `kubectl explain cluster.spec.externalClusters` and
> `kubectl explain cluster.spec.replica`. Until confirmed, treat `-8`'s
> `wal_keep_size` as the primary retention guarantee and the manual slot below
> as defence-in-depth.

**ACTION** (human, on `-8`):

```bash
# 1) bound slot retention so no slot can fill the disk (tune to taste)
kubectl -n totallylegitco exec fhi-pg-main-8-3 -c postgres -- \
  psql -U postgres -c "ALTER SYSTEM SET max_slot_wal_keep_size = '80GB';"
# 2) keep a generous WAL floor for the clone window
kubectl -n totallylegitco exec fhi-pg-main-8-3 -c postgres -- \
  psql -U postgres -c "ALTER SYSTEM SET wal_keep_size = '8GB';"
kubectl -n totallylegitco exec fhi-pg-main-8-3 -c postgres -- \
  psql -U postgres -c "SELECT pg_reload_conf();"
# NOTE: prefer setting these via -8's CNPG spec.postgresql.parameters so they
# survive a pod recreate; ALTER SYSTEM here is the fast path for the window.

# 3) defence-in-depth slot dedicated to -9
kubectl -n totallylegitco exec fhi-pg-main-8-3 -c postgres -- psql -U postgres -c \
  "SELECT pg_create_physical_replication_slot('fhi_pg9_migration_slot', true);"
```

**VALIDATION**

```bash
./scripts/check-pg8-source.sh
```

**GATE:** `check-pg8-source.sh` prints `SOURCE PREFLIGHT PASSED` — free normal
connection slots ≥ threshold, `max_wal_senders`/`max_replication_slots` have
headroom, and `max_slot_wal_keep_size` is **bounded** (not `-1`).

---

## Phase 2 — Safety-net logical dump of `-8`

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

## Phase 3 — Apply the ObjectStore, then the Cluster

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

## Phase 4 — Verify streaming + slot

**ACTION / VALIDATION**

```bash
./scripts/check-pg9-replication.sh
```

**GATE:** `-9 REPLICATION HEALTHY` — `-8` shows `-9` `state=streaming`, a physical
slot is active on `-8`, `pg_is_in_recovery()=t` on `-9`, replay lag is bounded,
and the `plugin-barman-cloud:v0.13` sidecar is injected in **every** `-9` pod.

---

## Phase 5 — Verify `-9` archiving + a fresh `-9` backup

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

## Phase 6 — Scale `1 → 2 → 3` with per-step validation

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

## Phase 7 — Data validation

**ACTION** (run when `-8` writes are quiet so counts are stable):

```bash
CRITICAL_TABLES="django_migrations auth_user <add-your-critical-tables>" \
  ./scripts/validate-pg8-vs-pg9.sh
```

**VALIDATION / GATE:** `DATA VALIDATION PASSED` — db list, roles, extensions,
**exact** `COUNT(*)` for critical tables, migration history, and app-role
presence all match between `-8` and `-9`. Investigate any mismatch before
cutover (transient sequence drift while `-8` still writes is reported as WARN,
not FAIL).

---

## Phase 8 — Quiesce, zero lag, promote

**Destructive / one-way-ish. Requires confirmation.**

**ACTION**

```bash
# 1) stop writes to -8 (app maintenance mode / scale app deploy to 0).
# 2) confirm zero replay lag on -9:
kubectl -n totallylegitco exec fhi-pg-main-9-1 -c postgres -- psql -U postgres -Atc \
 "SELECT pg_wal_lsn_diff(pg_last_wal_receive_lsn(), pg_last_wal_replay_lsn());"   # want 0
# 3) promote -9 (sibling PR script; self-gates on lag==0 and single-primary):
./scripts/promote-pg9.sh
```

**VALIDATION**

```bash
kubectl -n totallylegitco exec fhi-pg-main-9-1 -c postgres -- psql -U postgres -Atc \
 "SELECT pg_is_in_recovery();"    # must now be 'f' (promoted primary)
kubectl -n totallylegitco get cluster fhi-pg-main-9
```

**GATE:** `pg_is_in_recovery()=f` on the new `-9` primary; `-9` is healthy; `-8`
is **read-only** / write-fenced (app still pointed away). **Never** promote with
non-zero lag or with `-8` still writable.

---

## Phase 9 — App cutover (keep `-8` as rollback)

**ACTION**

```bash
./scripts/cutover-app-to-pg9.sh      # repoints DATABASE_URL / rw service to -9
```

**VALIDATION / GATE:** app health checks green against `-9`; write a canary row
and read it back; error rates normal. **Keep `-8` intact and write-fenced** as
the rollback target — do NOT decommission `-8` yet.

---

## Phase 10 — Rollback semantics

Choose by whether the app has written to `-9` since cutover:

- **Pre-write** (no writes hit `-9` yet — trivial): point the app back at `-8`.
  ```bash
  ./scripts/rollback-pre-write-cutover.sh
  ```
  `-8` was only write-fenced, not changed; this is the safe, fast path.
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

1. External-primary physical slot binding for a replica cluster
   (`cluster.spec.externalClusters`, `cluster.spec.replica`). Phase 1c.
2. Managed roles + superuser reconciliation timing on a read-only replica
   (`cluster.spec.managed.roles`, `cluster.spec.superuserSecret`). Phase 3.
3. `archive_mode on` vs `always` for a replica archiving its own WAL pre-
   promotion. Phase 5.
