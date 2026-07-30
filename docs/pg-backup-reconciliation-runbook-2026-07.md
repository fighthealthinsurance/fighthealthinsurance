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
   (readiness follows leadership), and with **two Services claiming plugin name
   `barman-cloud.cloudnative-pg.io`** the operator's plugin discovery/gRPC is
   broken → reconciliation blocked ever since. This is a **recurrence of the -8
   outage pattern** (see `docs/pg-reliability-hardening.md`, Fault A: duplicate
   installs + stale lease + dead Service endpoints), just without the cert break.
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
`scripts/check-pg9-backup-health.sh` (new), and in `colo-scripts`: pinned
chart versions + `cleanupbooks/cnpg-backup-cruft.yaml`.

---

## Fast path (if you already know the story)

```bash
# 1. cruft (dead fhi-pg-main-7 backups — unstarves the scheduling controller)
kubectl -n default delete scheduledbackup backup-example
kubectl -n default delete backup --all                      # all target dead -7; CRs only, no bucket data

# 2. converge to ONE plugin install (keep helm, drop the manifest one)
kubectl -n cnpg-system delete deployment barman-cloud service barman-cloud
kubectl -n cnpg-system delete lease 822e3f5c.cnpg.io       # frees leadership immediately
kubectl -n cnpg-system get pods -w                          # helm pod -> 1/1

# 3. reconciliation clears (restart operator only if it doesn't within ~3m)
kubectl -n totallylegitco get cluster fhi-pg-main-9 -w

# 4. backups: prove once, then schedule daily
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

## Phase 2 — converge to ONE plugin install (keep helm, remove manifest)

We keep the **helm** release: it is what git (`colo-scripts`) manages, it is
the same app version (v0.13.0) as the manifest install, and future upgrades
become a reviewable one-line pin bump.

```bash
# GUARDS (all must hold before deleting anything):
helm -n cnpg-system list                                   # releases: cnpg, barman-cloud
helm -n cnpg-system get manifest barman-cloud | grep -B2 -A6 '^kind: \(Deployment\|Service\)$' \
  | grep -E 'kind:|name:'
#   -> the release must own barman-cloud-plugin-barman-cloud, and must NOT own
#      the plain names deleted below.
kubectl -n cnpg-system get deploy barman-cloud \
  -o jsonpath='helm-owner={.metadata.annotations.meta\.helm\.sh/release-name}{"\n"}'
#   -> must print 'helm-owner=' (EMPTY: manifest-based, safe to delete)

kubectl -n cnpg-system delete deployment barman-cloud service barman-cloud
kubectl -n cnpg-system delete lease 822e3f5c.cnpg.io       # just a lock; recreated on acquire
kubectl -n cnpg-system get pods -w
```

**GATE:** `barman-cloud-plugin-barman-cloud-*` reaches **1/1**; its log shows
`successfully acquired lease`; and its Service has ready endpoints (the -8
selector-break check):

```bash
kubectl -n cnpg-system logs deploy/barman-cloud-plugin-barman-cloud | tail -5
kubectl -n cnpg-system get endpointslices -l kubernetes.io/service-name=barman-cloud-plugin-barman-cloud
```

Then reconciliation should clear on its own:

```bash
kubectl -n totallylegitco get cluster fhi-pg-main-9 -w    # -> "Cluster in healthy state"
# only if still plugin-errored after ~3 minutes:
kubectl -n cnpg-system rollout restart deploy cnpg-cloudnative-pg
```

Leftover manifest RBAC/certs are inert; clean them in Phase 6, **never** the
`barmancloud.cnpg.io` CRDs (deleting the ObjectStore CRD would cascade-delete
`fhi-backup-store-9`).

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
# leftover manifest-install RBAC/certs (inert; delete only what is NOT
# helm-annotated and NOT a CRD):
kubectl -n cnpg-system get sa,role,rolebinding,certificate,issuer,secret -o name | grep -i barman
kubectl get clusterrole,clusterrolebinding -o name | grep -i barman
#   check each:  -o jsonpath='{.metadata.annotations.meta\.helm\.sh/release-name}'
#   empty -> manifest leftover -> deletable.  NEVER delete the
#   barmancloud.cnpg.io CRDs (cascade-deletes fhi-backup-store-9).

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
