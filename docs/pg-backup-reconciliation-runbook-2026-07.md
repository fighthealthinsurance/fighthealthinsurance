# July 2026 — fhi-pg-main-9 reconciliation blocked + backups stale: runbook

Live remediation for the state observed 2026-07-30:

| Symptom | Evidence |
|---|---|
| `fhi-pg-main-9` status: *"Cluster cannot proceed to reconciliation due to an error while interacting with plugins"* | `kubectl get cluster -A` (DB itself is up, 3/3) |
| **Two** barman-cloud plugin installs in `cnpg-system` | deploy `barman-cloud` (manifest-based, 1/1, holds the leader lease) **and** deploy `barman-cloud-plugin-barman-cloud` (helm, **0/1 for 12d**, log stuck at *"Attempting to acquire leader lease"* on `822e3f5c.cnpg.io`) |
| Newest `-9` base backup 19d old; no ScheduledBackup exists for `-9` | `kubectl -n totallylegitco get backup` |
| ~240 forever-`pending` `backup-example-*` Backups + a `backup-example` ScheduledBackup in `default`, all targeting the **dead** `fhi-pg-main-7` | `kubectl get backup -A`; operator webhook log still shows create-attempts for months-old `backup-example-*` names **today** |
| **Zero** ScheduledBackup-created Backups cluster-wide since `20260718000000` (both `backup-example-*` and `pcfweb-pg-nightly-*` stop there) | `kubectl get backup -A` |
| `pcfweb-pg` (PG **18**, in-tree `barmanObjectStore`) stuck `walArchivingFailing`; its nightlies all `pending` | `kubectl -n pcfweb describe backup pcfweb-pg-backup-manual` |

## What happened

1. **~Jul 10–11**: plugin-barman-cloud **v0.13.0** installed via the release
   *manifest* (`kubectl apply -f .../manifest.yaml`) → deployment/Service named
   `barman-cloud` in `cnpg-system`. `-9` was created Jul 11 and its manual
   backup completed through this install.
2. **Jul 18 ~19:00 UTC**: `colo-scripts/playbooks/cluster-setup.yaml` ran. Its
   two CNPG helm tasks were **unpinned**, so it (a) upgraded the operator to
   chart 0.29.0 / **operator 1.30.0** (new `cnpg-cloudnative-pg` pod), and
   (b) installed a **second copy of the plugin** as helm release `barman-cloud`
   (chart 0.7.0 / app v0.13.0 — helm names it `barman-cloud-plugin-barman-cloud`).
3. The helm pod can never acquire the plugin's leader-election Lease
   (`822e3f5c.cnpg.io`) — the manifest pod holds it — so it sits **0/1**
   (readiness follows leadership). Worse, the two install methods **share
   resource names**: the chart hardcodes Service `barman-cloud` (*"DO NOT
   CHANGE THE SERVICE NAME"* — it must match the serving cert), so the Jul 18
   helm install re-pointed the ONE existing `barman-cloud` Service at its own
   — not-ready — pod, leaving the operator's gRPC/mTLS plugin channel with no
   ready backend → reconciliation blocked ever since. This is a **recurrence
   of the -8 outage pattern** (see `docs/pg-reliability-hardening.md`, Fault
   A: duplicate installs + stale lease + a Service selecting zero ready pods),
   just without the cert break.
4. Independently, the `backup-example` ScheduledBackup (a CNPG docs example
   applied ~Nov 2025, targeting the long-dead `fhi-pg-main-7`) has left the new
   operator in a **catch-up storm**: every reconcile re-attempts creation of
   ~240 already-existing daily Backups (visible in the webhook log). Since the
   Jul 18 operator restart, **no ScheduledBackup anywhere has produced a new
   Backup** — the storm/wedge starves the scheduling controller. This is why
   pcfweb's nightlies stopped on exactly Jul 18.
5. `-9` itself never had a ScheduledBackup: `k8s/fhi-pg-main-9-alerts.yaml`
   pages on backup age >26h *assuming* a daily schedule that was never created
   (only the migration's manual Phase-3 backup existed).

Root causes: **(a)** duplicate plugin installs (manifest + helm), **(b)**
unpinned helm charts in the cluster playbook, **(c)** orphaned docs-example
ScheduledBackup aimed at a dead cluster, with no owner-refs on its Backups,
**(d)** missing `-9` ScheduledBackup.

Fixes in-repo: `k8s/fhi-pg-main-9-scheduledbackup.yaml` (new),
`scripts/check-pg9-backup-health.sh` (new), `scripts/build.sh` (now re-asserts
the schedule on every deploy), and in `colo-scripts`: pinned chart versions +
`cleanupbooks/cnpg-backup-cruft.yaml` (convergent cleanup + reinstall).

---

## Fast path (if you already know the story)

```bash
# 1. cruft (dead fhi-pg-main-7 backups — unstarves the scheduling controller)
kubectl -n default delete scheduledbackup backup-example
kubectl -n default delete backup --all                      # all target dead -7; CRs only, no bucket data

# 2. converge to ONE plugin install: uninstall + purge + clean pinned reinstall.
#    Do NOT delete piecemeal by name — Service/Certificate/ConfigMap names are
#    SHARED between the install methods (Phase 2 explains what that broke).
helm -n cnpg-system uninstall barman-cloud || true
for r in $(kubectl -n cnpg-system get deploy,svc,cm,sa,role,rolebinding,certificate,issuer -o name | grep -i barman); do kubectl -n cnpg-system delete "$r"; done
for r in $(kubectl get clusterrole,clusterrolebinding -o name | grep -i barman); do kubectl delete "$r"; done
helm repo update cnpg
helm -n cnpg-system install barman-cloud cnpg/plugin-barman-cloud --version 0.7.0 --wait --timeout 5m

# 3. reconciliation clears (restart operator only if it doesn't within ~3m)
kubectl -n totallylegitco get cluster fhi-pg-main-9 -w

# 4. backups: prove once, then schedule daily. If the archiver is stuck on
#    000000010000000000000001 / archived_count=0 -> Phase 3a (never worked).
kubectl apply -f k8s/fhi-pg-main-9-scheduledbackup.yaml     # immediate: true takes one now
./scripts/check-pg9-backup-health.sh
```

Details, guards, and validation below — use the phases if anything deviates.

---

## Phase 0 — capture state, assess urgency (read-only)

```bash
kubectl -n cnpg-system get deploy,pods,svc,lease -o wide
kubectl -n cnpg-system get lease 822e3f5c.cnpg.io -o jsonpath='{.spec.holderIdentity}{"\n"}'
kubectl -n totallylegitco get cluster fhi-pg-main-9 -o jsonpath='{.status.phase}: {.status.phaseReason}{"\n"}'
# Is WAL actually piling up / is the disk at risk? (archiving may still work --
# the instance SIDECARS archive; it is the operator<->plugin channel that broke)
kubectl -n totallylegitco exec fhi-pg-main-9-1 -c postgres -- psql -U postgres -xc \
  "SELECT archived_count, failed_count, last_archived_time, last_failed_wal FROM pg_stat_archiver;"
for p in fhi-pg-main-9-1 fhi-pg-main-9-2 fhi-pg-main-9-3; do
  kubectl -n totallylegitco exec "$p" -c postgres -- sh -c \
    'echo "$HOSTNAME: $(ls /var/lib/postgresql/data/pgdata/pg_wal | grep -Ec "^[0-9A-F]{24}$") segments"; df -h /var/lib/postgresql/data | tail -1'
done
```

**Read it like this:** `last_archived_time` recent + `failed_count` flat ⇒ WAL
archiving is fine and this is "only" a reconciliation/backup outage. WAL
segments climbing toward 640 (10GiB) / 1280 (20GiB) or volume ≥70% ⇒ move
straight to Phase 2 now. **Never hand-delete anything in `pg_wal`.**

## Phase 1 — delete the dead-cluster backup cruft in `default`

Why first: it is zero-risk (CR deletion never touches object-store data, and
`fhi-pg-main-7` is dead anyway) and it unstarves the ScheduledBackup
controller, which you need working in Phase 3.

```bash
# guard: every Backup in default must target fhi-pg-main-7
kubectl -n default get backup \
  -o custom-columns='NAME:.metadata.name,CLUSTER:.spec.cluster.name' --no-headers \
  | awk '$2!="fhi-pg-main-7"{print "UNEXPECTED:", $0}'
# expect NO output; then:
kubectl -n default delete scheduledbackup backup-example
kubectl -n default delete backup --all        # ~241 objects, takes a minute
```

**VALIDATION:** operator log goes quiet on `backup-example`:
`kubectl -n cnpg-system logs deploy/cnpg-cloudnative-pg --since=10m | grep -c backup-example` → `0`.

## Phase 2 — converge to ONE plugin install (helm-managed, reinstalled clean)

We keep the **helm** release: it is what git (`colo-scripts`) manages, it is
the same app version (v0.13.0) as the manifest install, and future upgrades
become a reviewable one-line pin bump.

**Do NOT delete the manifest install piecemeal by name** — several names are
IDENTICAL across the two install methods: the chart hardcodes Service
`barman-cloud` (its values.yaml: *"DO NOT CHANGE THE SERVICE NAME as it is
currently used to generate the certificate"*), Certificates
`barman-cloud-client`/`barman-cloud-server`, ConfigMap
`plugin-barman-cloud-config`. Name-based deletion takes out shared objects the
survivor needs. As executed on 2026-07-30 this is exactly what bit us:
deleting `deployment/barman-cloud` freed the lease and the helm pod went
Ready, but `service/barman-cloud` was the ONE shared discovery Service — with
it gone the operator had no plugin endpoint at all, and the cluster stayed
plugin-errored even with a perfectly healthy plugin pod (leader, gRPC up,
ObjectStores reconciling).

Safe convergence = uninstall + purge + clean pinned reinstall. Safety facts:
the ObjectStore **CRD carries `helm.sh/resource-policy: keep`**, so neither
`helm uninstall` nor this purge can cascade-delete `fhi-backup-store-9`; the
purge also skips CRDs and Secrets explicitly (cert-manager re-issues the TLS
secrets from the fresh Certificates).

```bash
helm -n cnpg-system uninstall barman-cloud || true

# purge every remaining plugin resource EXCEPT CRDs + Secrets:
for r in $(kubectl -n cnpg-system get deploy,svc,cm,sa,role,rolebinding,certificate,issuer -o name | grep -i barman); do
  kubectl -n cnpg-system delete "$r"; done
for r in $(kubectl get clusterrole,clusterrolebinding -o name | grep -i barman); do
  kubectl delete "$r"; done

kubectl get crd objectstores.barmancloud.cnpg.io      # still present (resource-policy: keep)
kubectl -n totallylegitco get objectstore             # fhi-backup-store + fhi-backup-store-9 intact

helm repo update cnpg
helm -n cnpg-system install barman-cloud cnpg/plugin-barman-cloud --version 0.7.0 --wait --timeout 5m
# if helm refuses over ownership of the pre-existing CRDs: add --set crds.create=false
```

(Codified: `ansible-playbook cleanupbooks/cnpg-backup-cruft.yaml` does the
teardown half — default-ns cruft + TOTAL plugin removal (helm release +
leftovers; never CRDs/Secrets). It deliberately installs nothing: re-run
`playbooks/cluster-setup.yaml` afterwards, whose pinned helm task performs
the same clean install as above.)

**GATE:** plugin deployment **1/1**, and the discovery Service is back with
ready endpoints (the -8 selector-break check):

```bash
kubectl -n cnpg-system get deploy barman-cloud-plugin-barman-cloud
kubectl -n cnpg-system get svc barman-cloud --show-labels   # cnpg.io/pluginName=barman-cloud.cloudnative-pg.io
kubectl -n cnpg-system get endpointslices -l kubernetes.io/service-name=barman-cloud
```

Then reconciliation should clear on its own:

```bash
kubectl -n totallylegitco get cluster fhi-pg-main-9 -w    # -> "Cluster in healthy state"
# only if still plugin-errored after ~3 minutes:
kubectl -n cnpg-system rollout restart deploy cnpg-cloudnative-pg
```

No cluster-spec change is needed afterwards: `spec.plugins[].name:
barman-cloud.cloudnative-pg.io` is the plugin's *protocol* name — the operator
matches it against the Service's `cnpg.io/pluginName` label, which is
identical under both install methods — and `barmanObjectName`/`serverName`
reference the ObjectStore CR, which the reinstall never touches.

## Phase 3 — re-prove `-9` backups end-to-end, then schedule them

Same proof as migration runbook Phase 3, now with real data:

```bash
kubectl -n totallylegitco exec fhi-pg-main-9-1 -c postgres -- psql -U postgres -c 'SELECT pg_switch_wal();'
sleep 30
kubectl -n totallylegitco exec fhi-pg-main-9-1 -c postgres -- psql -U postgres -xc \
  "SELECT archived_count, failed_count, last_archived_time FROM pg_stat_archiver;"   # rising / flat / fresh

kubectl apply -f k8s/fhi-pg-main-9-scheduledbackup.yaml
# immediate: true -> a Backup appears right away:
kubectl -n totallylegitco get backup -l cnpg.io/cluster=fhi-pg-main-9 \
  --sort-by=.metadata.creationTimestamp -w                                            # -> completed
aws --endpoint-url https://s3.us-west-004.backblazeb2.com s3 ls \
  s3://fhi-pg-backup-second/fhi-pg-main-9/base/ | tail
./scripts/check-pg9-backup-health.sh                                                  # ALL CHECKS PASSED
```

If archiving is broken at the instance level even though the plugin is
healthy, the sidecars may need re-injection: restart instances one at a time,
replicas first (`primaryUpdateStrategy: supervised` means nothing rolls
without you): `kubectl cnpg restart fhi-pg-main-9 -n totallylegitco` — or
plugin-free, delete pods `-2`/`-3` one at a time, then switchover, then the
old primary.

### Phase 3a — the archiver has NEVER worked (stuck on `…0001`)

Observed live 2026-07-31 in the `fhi-pg-main-9-1` instance log:

```
Error while calling ArchiveWAL … unexpected failure invoking barman-cloud-wal-archive: exit status 1
The failed archive command was: /controller/manager wal-archive … pg_wal/000000010000000000000001
```

`000000010000000000000001` is the **first segment of timeline 1**. Postgres
archives strictly in order, so still failing on segment #1 means `-9` has
archived **zero WAL since its Jul 11 birth** — independent of (and predating)
the duplicate-install outage: the WAL path is instance manager → **local
sidecar** → B2 and never touches the central plugin deployment. Consequences:

- **pg_wal grows without bound.** Unarchived WAL is exempt from
  `max_slot_wal_keep_size`; this is the exact mechanism that filled and
  killed `-8-1`. Run the Phase 0 WAL-count/df block IMMEDIATELY.
- The Jul 11 "completed" base backup is **probably not restorable**: a
  barman base backup needs the archived WALs spanning its start/stop to
  reach consistency, and the archive has none. Treat `-9` as having NO
  usable backup until archiving works, a fresh backup completes, AND a
  restore drill passes.

The real error text lives in the sidecar container on the primary:

```bash
kubectl -n totallylegitco logs fhi-pg-main-9-1 -c plugin-barman-cloud --since=30m | tail -30
```

Likely causes, in rough order:

1. **Empty-WAL-archive check failing — CONFIRMED live 2026-08-04** (sidecar
   log: `ERROR: WAL archive check failed for server fhi-pg-main-9: Expected
   empty archive`, retried every ~60s). On first archive the plugin verifies
   the `wals/` prefix for `serverName: fhi-pg-main-9` is EMPTY (that is what
   the `.check-empty-wal-archive` marker in PGDATA is about). An earlier
   `-9` incarnation or restore-drill cluster (`pg-copy.yaml` /
   `pg-recover.yaml`) archived under the same serverName before this
   cluster's 2026-07-11 birth, so the check fails forever. Fix by clearing
   ONLY the `wals/` prefix (leave `base/`):

   ```bash
   B2="--endpoint-url https://s3.us-west-004.backblazeb2.com"
   # sanity: size + dates — the content must all predate 2026-07-11
   aws $B2 s3 ls --recursive --summarize s3://fhi-pg-backup-second/fhi-pg-main-9/wals/ | tail -5
   # preferred: park it rather than destroy it
   aws $B2 s3 mv --recursive s3://fhi-pg-backup-second/fhi-pg-main-9/wals/ \
     s3://fhi-pg-backup-second/graveyard/fhi-pg-main-9-stale-wals/
   # (aws s3 rm --recursive on wals/ is fine too once the dates are confirmed)
   ```

   The next ~60s retry passes the check — watch the sidecar log flip to
   success and `pg_stat_archiver.archived_count` start rising; pg_wal on the
   primary then drains as the backlog uploads. This fix is INDEPENDENT of
   the plugin Service/reconciliation repair — archiving resumes even before
   the reinstall, because the WAL path is instance → local sidecar → B2.

   **RESOLVED 2026-08-04:** the blocking content was 8 objects on timeline
   4 (`wals/0000000400000003/…`) dated 2026-01-05 — a January incarnation
   archiving under the same serverName — now parked in
   `graveyard/fhi-pg-main-9-stale-wals/`. Archiving started within a minute
   of the move (`archived_count` rising from 0, fresh `last_archived_time`).
   `failed_count` ≈ 100k is the 24-day retry history — it is a lifetime
   counter and never resets; only a rising trend is a problem.
   **Never "fix" this by deleting the `.check-empty-wal-archive` marker or
   otherwise skipping the check** — it exists to stop two clusters from
   interleaving WAL in one archive, which silently corrupts the restore
   lineage.
2. **B2 checksum env vars missing from the RUNNING sidecars.** The
   ObjectStore sets `AWS_REQUEST_CHECKSUM_CALCULATION` /
   `AWS_RESPONSE_CHECKSUM_VALIDATION`, but sidecar env is baked at pod
   creation — pods older than the ObjectStore change never picked them up:
   `kubectl -n totallylegitco get pod fhi-pg-main-9-1 -o jsonpath='{.spec.containers[?(@.name=="plugin-barman-cloud")].env}'`
   If absent, restart instances one at a time (supervised) to re-inject.
3. **Credentials** (`pg-backup2`) — the sidecar log will show S3 403s.

After the fix, validate recovery by `pg_stat_archiver.archived_count` RISING
with a fresh `last_archived_time`. Do NOT wait for `last_failed_wal` /
`failed_count` to clear — they persist until an explicit stats reset and
never clear on success; only their RECENCY vs the last success matters.
Then take the fresh backup (Phase 3 above) and run a restore drill before
trusting it.

## Phase 4 — pcfweb-pg: dead in-tree backup path (separate fix)

`pcfweb-pg` is PG **18** using the **in-tree** `barmanObjectStore` — deprecated
since CNPG 1.26 and removed in 1.28+ (the operator is now **1.30**), and the
PG18 operand images no longer ship the barman-cloud binaries. Its
`walArchivingFailing` has been failing **since creation** (first manual backup
Jul 11 already hit it): it has **zero working backups and no WAL archive**.

```bash
# confirm the archive failure mode:
kubectl -n pcfweb logs pcfweb-pg-1 | grep -iE 'archive|barman' | tail -20
kubectl -n pcfweb exec pcfweb-pg-1 -c postgres -- psql -U postgres -xc \
  "SELECT archived_count, failed_count, last_failed_wal FROM pg_stat_archiver;"
# stop the nightly from minting new doomed Backups until migrated:
kubectl -n pcfweb patch scheduledbackup pcfweb-pg-nightly --type=merge -p '{"spec":{"suspend":true}}'
```

**Fix = migrate it to the plugin**, exactly like `-9` (its manifests live
outside this repo — apply these, then commit them wherever pcfweb is managed):

```yaml
apiVersion: barmancloud.cnpg.io/v1
kind: ObjectStore
metadata:
  name: pcfweb-backup-store
  namespace: pcfweb
spec:
  configuration:
    destinationPath: s3://pcfweb-pg-backup/          # same bucket it always pointed at
    endpointURL: https://s3.us-west-004.backblazeb2.com
    s3Credentials:
      accessKeyId: {name: pg-backup, key: PG_ACCESS_KEY_ID}
      secretAccessKey: {name: pg-backup, key: PG_ACCESS_SECRET_KEY}
    data: {compression: gzip}
    wal: {compression: gzip, maxParallel: 4}
  retentionPolicy: "30d"
  # B2 rejects the AWS SDK's newer default checksums -- REQUIRED, same as -9:
  instanceSidecarConfiguration:
    env:
      - {name: AWS_REQUEST_CHECKSUM_CALCULATION, value: when_required}
      - {name: AWS_RESPONSE_CHECKSUM_VALIDATION, value: when_required}
```

Cluster edit (in pcfweb's manifest): **remove** `spec.backup.barmanObjectStore`,
add:

```yaml
  plugins:
    - name: barman-cloud.cloudnative-pg.io
      isWALArchiver: true
      parameters:
        barmanObjectName: pcfweb-backup-store
        serverName: pcfweb-pg
```

Then: delete the stale `pending`/`walArchivingFailing` pcfweb Backups, take a
manual `method: plugin` Backup to `completed`, and recreate
`pcfweb-pg-nightly` with `method: plugin` + `pluginConfiguration` +
`backupOwnerReference: self` (schedule `"0 0 9 * * *"` keeps it off `-9`'s
08:00 slot).

## Phase 5 — `mautrix-discord-db` has no backups at all

`matrix/mautrix-discord-db` (144d) has **zero** Backup/ScheduledBackup
configured. Decide deliberately: if losing the Discord-bridge state is
acceptable, record that; otherwise give it the same ObjectStore+plugin+
ScheduledBackup treatment (its own bucket prefix / serverName).

## Phase 6 — cosmetic cleanup + keeping it fixed

```bash
# after Phase 2's purge + reinstall, every plugin resource left in cnpg-system
# must carry meta.helm.sh/release-name=barman-cloud; anything without it is a
# leftover -> deletable. NEVER delete the barmancloud.cnpg.io CRDs
# (cascade-deletes fhi-backup-store-9).
kubectl -n cnpg-system get deploy,svc,cm,sa,role,rolebinding,certificate,issuer,secret -o name | grep -i barman

kubectl -n totallylegitco get prometheusrule fhi-pg-main-9-backup-wal   # alerts actually applied?
```

- `colo-scripts` now pins `cloudnative-pg` **0.29.0** and `plugin-barman-cloud`
  **0.7.0** with `wait: yes` (an install stuck 0/1 fails the playbook loudly
  instead of rotting for 12 days). Upgrades are deliberate pin bumps: check the
  plugin release notes for sidecar/selector changes first (the v0.9→v0.13
  break), and bump plugin + operator separately.
- **Never** `kubectl apply` the plugin's release `manifest.yaml` again — that
  is how the duplicate was born. Helm (via the playbook) is the only installer.
- `./scripts/check-pg9-backup-health.sh` is the one-shot health probe for all
  of the above; run it after any operator/plugin change and whenever backups
  feel doubtful. The 26h-backup-age and WAL alerts in
  `k8s/fhi-pg-main-9-alerts.yaml` are the always-on version.

### Redeploy idempotency — a redeploy can never mint a second install

- **colo-scripts** `playbooks/cluster-setup.yaml`: the plugin is exactly ONE
  pinned helm release (`barman-cloud`, chart 0.7.0). Re-running the playbook
  `helm upgrade`s that release in place — it cannot create a second copy — and
  `wait: yes` fails the run loudly if the rollout wedges (a 0/1 pod would have
  failed the Jul 18 run instead of rotting for 12 days). The only path back to
  two installs is hand-applying the GitHub release `manifest.yaml` — don't;
  the health script (checks 1–3) detects that state, and
  `cleanupbooks/cnpg-backup-cruft.yaml` (total teardown) + a
  `cluster-setup.yaml` re-run converge it.
- **fighthealthinsurance** `scripts/build.sh`: never touches the plugin. It
  re-asserts `k8s/db-config.yaml` and `k8s/fhi-pg-main-9-scheduledbackup.yaml`
  on every deploy (idempotent applies), so an app redeploy self-heals the
  backup schedule and cannot affect the plugin install.
- The `-9` cluster spec needs no per-install-method changes ever:
  `spec.plugins[].name` is the plugin's protocol name (matched via the
  Service's `cnpg.io/pluginName` label, identical under both install methods)
  and `barmanObjectName`/`serverName` reference the ObjectStore CR, which
  installs/uninstalls never touch.
