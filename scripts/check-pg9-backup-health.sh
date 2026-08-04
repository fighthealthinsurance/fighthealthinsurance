#!/usr/bin/env bash
# =============================================================================
# check-pg9-backup-health.sh
# Verify the ENTIRE fhi-pg-main-9 backup path is healthy: exactly one
# barman-cloud plugin install, operator<->plugin channel up, archiver
# advancing, WAL bounded, base backups fresh, and no ScheduledBackup cruft.
#
# Encodes the lessons of BOTH incidents:
#   - the -8 outage (v0.9->v0.13: duplicate plugin installs, stale leader
#     lease, empty Service endpoints -> reconciliation stalled), and
#   - the July 2026 -9 recurrence (helm release + manifest install both
#     present; `backup-example` ScheduledBackup graveyard in `default`
#     starving scheduling cluster-wide).
#
# Checks (all READ-ONLY):
#   1. exactly ONE plugin-barman-cloud deployment in cnpg-system, fully ready
#   2. the plugin leader lease is held by a pod of that deployment
#   3. plugin Service discoverable + endpoints non-empty (the -8 selector break)
#   4. cluster status is healthy (not "error while interacting with plugins")
#   5. WAL archiver advancing on the primary (matches FhiPg9ArchiverStalled)
#   6. pg_wal segment count bounded on every instance (matches 10/20GB alerts)
#   7. PGDATA volume headroom on every instance
#   8. newest completed base backup younger than 26h (matches FhiPg9BackupTooOld)
#   9. ScheduledBackup exists, is not suspended, and matches the manifest spec
#  10. cluster-wide cruft scan: ScheduledBackups aimed at dead clusters,
#      long-pending Backup CRs (the backup-example failure mode)
#
# Usage: ./scripts/check-pg9-backup-health.sh
#   (kube-context must point at the prod cluster; all knobs env-overridable)
# =============================================================================
set -euo pipefail

NAMESPACE="${NAMESPACE:-totallylegitco}"
CLUSTER="${CLUSTER:-fhi-pg-main-9}"
PG_CONTAINER="${PG_CONTAINER:-postgres}"
PLUGIN_NS="${PLUGIN_NS:-cnpg-system}"
PLUGIN_NAME="${PLUGIN_NAME:-barman-cloud.cloudnative-pg.io}"
# Deployment the *helm release* owns (release "barman-cloud" x chart
# "plugin-barman-cloud" -> fullname below). Anything ELSE running the plugin
# image is a duplicate install.
PLUGIN_DEPLOY="${PLUGIN_DEPLOY:-barman-cloud-plugin-barman-cloud}"
# Leader-election lock the plugin hardcodes (visible in its logs).
LEASE_NAME="${LEASE_NAME:-822e3f5c.cnpg.io}"
PGDATA="${PGDATA:-/var/lib/postgresql/data/pgdata}"
MAX_ARCHIVE_AGE_S="${MAX_ARCHIVE_AGE_S:-3600}"     # FhiPg9ArchiverStalled
WAL_WARN_SEGS="${WAL_WARN_SEGS:-640}"              # 10GiB @ 16MB
WAL_FAIL_SEGS="${WAL_FAIL_SEGS:-1280}"             # 20GiB @ 16MB
MAX_BACKUP_AGE_S="${MAX_BACKUP_AGE_S:-93600}"      # 26h, FhiPg9BackupTooOld
SCHEDULED_BACKUP="${SCHEDULED_BACKUP:-fhi-pg-main-9-nightly}"
EXPECTED_SCHEDULE="${EXPECTED_SCHEDULE:-0 0 8 * * *}"  # must match the manifest
PENDING_CRUFT_AGE_S="${PENDING_CRUFT_AGE_S:-172800}"  # pending >48h = cruft

info() { printf '\033[0;36m[INFO]\033[0m %s\n' "$*"; }
pass() { printf '\033[0;32m[PASS]\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m[WARN]\033[0m %s\n' "$*"; }
fail() { printf '\033[0;31m[FAIL]\033[0m %s\n' "$*"; FAILED=$((FAILED+1)); }
die()  { printf '\033[0;31m[FAIL]\033[0m %s\n' "$*"; exit 1; }

FAILED=0

command -v kubectl >/dev/null 2>&1 || die "kubectl not found on PATH"
CTX="$(kubectl config current-context 2>/dev/null || true)"
[ -n "$CTX" ] || die "no active kube-context"
info "kube-context : $CTX"
info "cluster      : $NAMESPACE/$CLUSTER   plugin ns: $PLUGIN_NS"

# --- 1. exactly ONE plugin deployment, fully ready ---------------------------
info "=== [1] single plugin-barman-cloud deployment ==="
mapfile -t PLUGIN_DEPLOYS < <(kubectl -n "$PLUGIN_NS" get deploy \
  -o custom-columns='NAME:.metadata.name,IMG:.spec.template.spec.containers[*].image' \
  --no-headers 2>/dev/null | awk '/plugin-barman-cloud/{print $1}')
if [ "${#PLUGIN_DEPLOYS[@]}" -eq 0 ]; then
  fail "no plugin-barman-cloud deployment found in $PLUGIN_NS"
elif [ "${#PLUGIN_DEPLOYS[@]}" -gt 1 ]; then
  fail "DUPLICATE plugin installs: ${PLUGIN_DEPLOYS[*]} -- they fight over the leader lease (the July 2026 reconciliation outage). Keep only the helm-managed '$PLUGIN_DEPLOY'; see docs/pg-backup-reconciliation-runbook-2026-07.md"
else
  if [ "${PLUGIN_DEPLOYS[0]}" = "$PLUGIN_DEPLOY" ]; then
    pass "exactly one plugin deployment: ${PLUGIN_DEPLOYS[0]}"
  else
    # a lone NON-helm install still violates the runbook end-state: the next
    # cluster-setup.yaml run would recreate the duplicate-install deadlock
    fail "single plugin deployment is '${PLUGIN_DEPLOYS[0]}' (expected helm-managed '$PLUGIN_DEPLOY')"
  fi
fi
for d in "${PLUGIN_DEPLOYS[@]}"; do
  READY="$(kubectl -n "$PLUGIN_NS" get deploy "$d" \
    -o jsonpath='{.status.readyReplicas}/{.status.replicas}' 2>/dev/null || echo '?/?')"
  if [ "$READY" = "?/?" ]; then
    fail "deployment $d: could not read readiness (kubectl error)"
  elif [ "${READY%/*}" = "${READY#*/}" ] && [ -n "${READY%/*}" ]; then
    pass "deployment $d ready: $READY"
  else
    fail "deployment $d NOT ready ($READY) -- likely waiting on the leader lease"
  fi
done

# --- 2. leader lease held by a live pod of the surviving deployment ----------
info "=== [2] plugin leader lease ==="
HOLDER="$(kubectl -n "$PLUGIN_NS" get lease "$LEASE_NAME" \
  -o jsonpath='{.spec.holderIdentity}' 2>/dev/null || true)"
if [ -z "$HOLDER" ]; then
  warn "lease $LEASE_NAME not found (ok only if the plugin was just reinstalled)"
else
  HOLDER_POD="${HOLDER%%_*}"
  if kubectl -n "$PLUGIN_NS" get pod "$HOLDER_POD" >/dev/null 2>&1; then
    pass "lease held by live pod $HOLDER_POD"
  else
    fail "lease $LEASE_NAME held by NON-EXISTENT pod '$HOLDER_POD' -- stale lock is blocking the plugin; kubectl -n $PLUGIN_NS delete lease $LEASE_NAME"
  fi
fi

# --- 3. plugin Service discovery + endpoints (the -8 selector break) ---------
info "=== [3] plugin Service + endpoints ==="
mapfile -t PLUGIN_SVCS < <(kubectl -n "$PLUGIN_NS" get svc \
  -l "cnpg.io/pluginName=$PLUGIN_NAME" -o name 2>/dev/null | sed 's|service/||')
if [ "${#PLUGIN_SVCS[@]}" -eq 0 ]; then
  # fall back to name-based discovery in case the label key changed upstream
  mapfile -t PLUGIN_SVCS < <(kubectl -n "$PLUGIN_NS" get svc -o name 2>/dev/null \
    | sed 's|service/||' | grep -i barman || true)
  if [ "${#PLUGIN_SVCS[@]}" -gt 0 ]; then
    warn "no Service labeled cnpg.io/pluginName=$PLUGIN_NAME; found by name: ${PLUGIN_SVCS[*]} -- verify the operator can discover the plugin"
  else
    fail "no plugin Service found in $PLUGIN_NS at all"
  fi
elif [ "${#PLUGIN_SVCS[@]}" -gt 1 ]; then
  fail "MULTIPLE Services claim plugin '$PLUGIN_NAME': ${PLUGIN_SVCS[*]} -- operator discovery is ambiguous (duplicate install)"
else
  pass "one plugin Service: ${PLUGIN_SVCS[0]}"
fi
for s in "${PLUGIN_SVCS[@]}"; do
  READY_EPS="$(kubectl -n "$PLUGIN_NS" get endpointslices \
    -l "kubernetes.io/service-name=$s" \
    -o jsonpath='{range .items[*]}{range .endpoints[*]}{.conditions.ready}{"\n"}{end}{end}' \
    2>/dev/null | grep -c true || true)"
  if [ "${READY_EPS:-0}" -ge 1 ]; then
    pass "service $s has $READY_EPS ready endpoint(s)"
  else
    fail "service $s has NO ready endpoints -- operator gRPC to the plugin has no backend (exact -8 failure)"
  fi
done

# --- 4. cluster reconciliation status ---------------------------------------
info "=== [4] cluster status ==="
PHASE="$(kubectl -n "$NAMESPACE" get cluster "$CLUSTER" \
  -o jsonpath='{.status.phase}' 2>/dev/null || true)"
PHASE_REASON="$(kubectl -n "$NAMESPACE" get cluster "$CLUSTER" \
  -o jsonpath='{.status.phaseReason}' 2>/dev/null || true)"
if [ "$PHASE" = "Cluster in healthy state" ]; then
  pass "cluster phase: $PHASE"
else
  fail "cluster phase: ${PHASE:-<none>} ${PHASE_REASON:+-- $PHASE_REASON}"
fi

PRIMARY="$(kubectl -n "$NAMESPACE" get cluster "$CLUSTER" \
  -o jsonpath='{.status.currentPrimary}' 2>/dev/null || true)"
[ -n "$PRIMARY" ] || die "cannot determine current primary for $CLUSTER"
info "primary      : $PRIMARY"

psql_primary() { kubectl -n "$NAMESPACE" exec -i "$PRIMARY" -c "$PG_CONTAINER" -- \
  psql -U postgres -d postgres -At -v ON_ERROR_STOP=1 -c "$1"; }

# --- 5. archiver advancing on the primary -----------------------------------
info "=== [5] WAL archiver on $PRIMARY ==="
ARCH="$(psql_primary "SELECT archived_count, failed_count,
  COALESCE(EXTRACT(EPOCH FROM (now()-last_archived_time))::bigint, -1),
  COALESCE(last_archived_wal,''), COALESCE(last_failed_wal,''),
  COALESCE(EXTRACT(EPOCH FROM (now()-last_failed_time))::bigint, -1)
  FROM pg_stat_archiver;")"
IFS='|' read -r ARCHIVED FAILED_N SINCE LAST_OK LAST_BAD SINCE_FAIL <<<"$ARCH"
info "archived_count=$ARCHIVED failed_count=$FAILED_N last_ok=$LAST_OK last_failed=$LAST_BAD"
if [ "$ARCHIVED" -eq 0 ]; then
  fail "archiver has NEVER succeeded on this timeline (archived_count=0)"
elif [ "$SINCE" -lt 0 ]; then
  fail "no last_archived_time reported"
elif [ "$SINCE" -gt "$MAX_ARCHIVE_AGE_S" ]; then
  fail "last successful archive was ${SINCE}s ago (> ${MAX_ARCHIVE_AGE_S}s) -- PITR coverage is stalled"
elif [ "$SINCE_FAIL" -ge 0 ] && [ "$SINCE_FAIL" -lt "$SINCE" ]; then
  # a failure NEWER than the last success = the archiver's most recent attempt
  # failed; postgres retries the same segment, so this is a live stall even if
  # the last success is only minutes old
  fail "archiver is CURRENTLY failing: last failure ${SINCE_FAIL}s ago (newer than last success ${SINCE}s ago), failing on '$LAST_BAD'"
else
  pass "last successful archive ${SINCE}s ago"
fi

# --- 6. pg_wal bounded on every instance ------------------------------------
info "=== [6] pg_wal segment count per instance ==="
mapfile -t PODS < <(kubectl -n "$NAMESPACE" get pods \
  -l "cnpg.io/cluster=$CLUSTER" -o name 2>/dev/null | sed 's|pod/||')
[ "${#PODS[@]}" -ge 1 ] || die "no pods found for cluster $CLUSTER"
for p in "${PODS[@]}"; do
  SEGS="$(kubectl -n "$NAMESPACE" exec "$p" -c "$PG_CONTAINER" -- \
    sh -c "ls '$PGDATA/pg_wal' 2>/dev/null | grep -Ec '^[0-9A-F]{24}\$'" || echo -1)"
  if [ "$SEGS" -lt 0 ]; then
    warn "$p: could not count WAL segments"
  elif [ "$SEGS" -gt "$WAL_FAIL_SEGS" ]; then
    fail "$p: $SEGS WAL segments (>20GiB) -- the -8-1 disk-full pattern; check archiver + stuck slots NOW"
  elif [ "$SEGS" -gt "$WAL_WARN_SEGS" ]; then
    warn "$p: $SEGS WAL segments (>10GiB early warning)"
  else
    pass "$p: $SEGS WAL segments"
  fi
done

# --- 7. PGDATA volume headroom ----------------------------------------------
info "=== [7] PGDATA volume usage per instance ==="
for p in "${PODS[@]}"; do
  USEP="$(kubectl -n "$NAMESPACE" exec "$p" -c "$PG_CONTAINER" -- \
    sh -c "df -P '$PGDATA' | awk 'NR==2{gsub(/%/,\"\",\$5); print \$5}'" 2>/dev/null || echo -1)"
  if [ "$USEP" -lt 0 ]; then
    warn "$p: could not read df for $PGDATA"
  elif [ "$USEP" -ge 85 ]; then
    fail "$p: PGDATA volume ${USEP}% full"
  elif [ "$USEP" -ge 70 ]; then
    warn "$p: PGDATA volume ${USEP}% full"
  else
    pass "$p: PGDATA volume ${USEP}% full"
  fi
done

# --- 8. newest completed base backup fresh enough ----------------------------
info "=== [8] base backup freshness ==="
NEWEST_DONE=""
while IFS=' ' read -r BNAME BPHASE BSTOP; do
  [ "$BPHASE" = "completed" ] && [ -n "$BSTOP" ] && [ "$BSTOP" != "<none>" ] && NEWEST_DONE="$BNAME $BSTOP"
done < <(kubectl -n "$NAMESPACE" get backup -l "cnpg.io/cluster=$CLUSTER" \
  --sort-by=.metadata.creationTimestamp \
  -o custom-columns='NAME:.metadata.name,PHASE:.status.phase,STOP:.status.stoppedAt' \
  --no-headers 2>/dev/null || true)
if [ -z "$NEWEST_DONE" ]; then
  fail "no COMPLETED backup exists for $CLUSTER"
else
  BNAME="${NEWEST_DONE% *}"; BSTOP="${NEWEST_DONE#* }"
  AGE_S=$(( $(date +%s) - $(date -d "$BSTOP" +%s) ))
  if [ "$AGE_S" -gt "$MAX_BACKUP_AGE_S" ]; then
    fail "newest completed backup '$BNAME' is ${AGE_S}s old (> ${MAX_BACKUP_AGE_S}s / 26h)"
  else
    pass "newest completed backup '$BNAME' is ${AGE_S}s old"
  fi
fi

# --- 9. ScheduledBackup present + active ------------------------------------
info "=== [9] ScheduledBackup ==="
if ! kubectl -n "$NAMESPACE" get scheduledbackup "$SCHEDULED_BACKUP" >/dev/null 2>&1; then
  fail "ScheduledBackup $SCHEDULED_BACKUP missing -- apply k8s/fhi-pg-main-9-scheduledbackup.yaml"
else
  SUSPENDED="$(kubectl -n "$NAMESPACE" get scheduledbackup "$SCHEDULED_BACKUP" \
    -o jsonpath='{.spec.suspend}' 2>/dev/null || true)"
  SB_GOT="$(kubectl -n "$NAMESPACE" get scheduledbackup "$SCHEDULED_BACKUP" \
    -o jsonpath='{.spec.cluster.name}|{.spec.method}|{.spec.pluginConfiguration.name}|{.spec.backupOwnerReference}|{.spec.schedule}' 2>/dev/null || true)"
  SB_WANT="$CLUSTER|plugin|$PLUGIN_NAME|self|$EXPECTED_SCHEDULE"
  if [ "$SUSPENDED" = "true" ]; then
    fail "ScheduledBackup $SCHEDULED_BACKUP is SUSPENDED"
  elif [ "$SB_GOT" != "$SB_WANT" ]; then
    fail "ScheduledBackup $SCHEDULED_BACKUP spec drift (cluster|method|plugin|ownerRef|schedule): got '$SB_GOT', want '$SB_WANT' -- re-apply k8s/fhi-pg-main-9-scheduledbackup.yaml"
  else
    pass "ScheduledBackup $SCHEDULED_BACKUP present, active, and matching the manifest"
  fi
fi

# --- 10. cluster-wide cruft scan (the backup-example failure mode) -----------
info "=== [10] cluster-wide ScheduledBackup/Backup cruft ==="
CRUFT=0
if ! SB_LIST="$(kubectl get scheduledbackup -A \
  -o custom-columns='NS:.metadata.namespace,NAME:.metadata.name,CLUSTER:.spec.cluster.name' \
  --no-headers 2>/dev/null)"; then
  fail "could not list ScheduledBackups cluster-wide (RBAC/API?) -- dead-cluster scan did NOT run"
  CRUFT=-1
else
  while IFS=' ' read -r SNS SNAME SCLUSTER; do
    [ -n "$SNS" ] || continue
    if ! kubectl -n "$SNS" get cluster "$SCLUSTER" >/dev/null 2>&1; then
      fail "ScheduledBackup $SNS/$SNAME targets NON-EXISTENT cluster '$SCLUSTER' -- delete it (its catch-up storm can starve ALL scheduled backups)"
      CRUFT=1
    fi
  done <<<"$SB_LIST"
fi

NOW_S="$(date +%s)"
PENDING_OLD=0
if ! B_LIST="$(kubectl get backup -A \
  -o custom-columns='NS:.metadata.namespace,NAME:.metadata.name,PHASE:.status.phase,CREATED:.metadata.creationTimestamp' \
  --no-headers 2>/dev/null)"; then
  fail "could not list Backups cluster-wide (RBAC/API?) -- pending-cruft scan did NOT run"
else
  while IFS=' ' read -r BNS BNAME BPHASE BCREATED; do
    [ -n "$BNS" ] || continue
    case "$BPHASE" in pending|walArchivingFailing) ;; *) continue ;; esac
    AGE=$(( NOW_S - $(date -d "$BCREATED" +%s) ))
    if [ "$AGE" -gt "$PENDING_CRUFT_AGE_S" ]; then
      PENDING_OLD=$((PENDING_OLD+1))
    fi
  done <<<"$B_LIST"
  if [ "$PENDING_OLD" -gt 0 ]; then
    # fail, not warn: long-pending Backup CRs are the exact failure mode that
    # starved ScheduledBackup processing cluster-wide in July 2026
    fail "$PENDING_OLD Backup CR(s) cluster-wide stuck pending/walArchivingFailing for >48h -- investigate/delete (kubectl get backup -A)"
  else
    pass "no long-pending Backup CRs cluster-wide"
  fi
fi
if [ "$CRUFT" -eq 0 ]; then
  pass "every ScheduledBackup targets a live cluster"
fi

# --- verdict -----------------------------------------------------------------
echo
if [ "$FAILED" -eq 0 ]; then
  pass "ALL CHECKS PASSED -- $CLUSTER backup path is healthy"
else
  die "$FAILED check(s) FAILED -- see docs/pg-backup-reconciliation-runbook-2026-07.md"
fi
