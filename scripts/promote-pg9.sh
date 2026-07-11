#!/bin/bash
#
# promote-pg9.sh -- Promote the fhi-pg-main-9 CNPG *replica cluster* to a primary.
#
# This is step 1 of the fhi-pg-main-8 -> fhi-pg-main-9 migration. It does NOTHING
# destructive until three preconditions hold and the operator types an explicit
# confirmation:
#
#   1. Application writes are QUIESCED on the source (-8): no active client
#      backends on the -8 primary.
#   2. Replay lag is ZERO: the -9 designated primary has replayed up to the -8
#      current WAL LSN (pg_current_wal_lsn == pg_last_wal_replay_lsn).
#   3. Explicit typed confirmation.
#
# CNPG 1.28 promotion semantics (verified against
# https://cloudnative-pg.io/docs/1.28/replica_cluster/ and .../kubectl-plugin/):
#
#   * `kubectl cnpg promote CLUSTER INSTANCE` is an INTRA-cluster switchover
#     (promotes a pod within one cluster). It is NOT how you promote a replica
#     *cluster*. This script never uses it.
#   * STANDALONE replica cluster  -> set `.spec.replica.enabled: false`. The
#     cluster exits continuous recovery and becomes an independent primary.
#     This is IRREVERSIBLE (source and target become two separate clusters).
#   * DISTRIBUTED topology (`.spec.replica.primary` set) -> a token dance: demote
#     the old primary to obtain `.status.demotionToken`, then set the new
#     cluster's `.spec.replica.primary` + `.spec.replica.promotionToken`
#     SIMULTANEOUSLY. Omitting the token triggers a failover requiring a rebuild
#     of the former primary. This script REFUSES to auto-run that path (it needs
#     the -8 manifest and cannot be validated offline) and instead prints the
#     documented procedure for a human.
#
# After promotion the script verifies pg_is_in_recovery() == false on -9.
#
# Env overrides:
#   NAMESPACE              (default totallylegitco)
#   SOURCE_CLUSTER         (default fhi-pg-main-8)
#   TARGET_CLUSTER         (default fhi-pg-main-9)  # evidence note calls it fhi-pg-9; override if so
#   EXPECTED_KUBE_CONTEXT  (optional; if set, the active context must match)
#   PROMOTE_TIMEOUT_SECS   (default 300) time to wait for -9 to leave recovery
#
set -euo pipefail

NAMESPACE="${NAMESPACE:-totallylegitco}"
SOURCE_CLUSTER="${SOURCE_CLUSTER:-fhi-pg-main-8}"
TARGET_CLUSTER="${TARGET_CLUSTER:-fhi-pg-main-9}"
PROMOTE_TIMEOUT_SECS="${PROMOTE_TIMEOUT_SECS:-300}"
KUBECTL_EXEC_TIMEOUT="${KUBECTL_EXEC_TIMEOUT:-30s}"   # bounds every kubectl exec so a hung pod can't stall the script
CONFIRM_PHRASE="promote ${TARGET_CLUSTER}"

RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; BLD=$'\033[1m'; RST=$'\033[0m'
info()  { printf '%s[promote]%s %s\n' "$BLD" "$RST" "$*"; }
ok()    { printf '%s[ PASS ]%s %s\n' "$GRN" "$RST" "$*"; }
warn()  { printf '%s[ WARN ]%s %s\n' "$YEL" "$RST" "$*" >&2; }
fail()  { printf '%s[ FAIL ]%s %s\n' "$RED" "$RST" "$*" >&2; exit 1; }

# --- preflight ---------------------------------------------------------------
command -v kubectl >/dev/null 2>&1 || fail "kubectl not found on PATH."

CTX="$(kubectl config current-context 2>/dev/null || true)"
[ -n "$CTX" ] || fail "No active kube context (kubectl config current-context is empty)."
if [ -n "${EXPECTED_KUBE_CONTEXT:-}" ] && [ "$CTX" != "$EXPECTED_KUBE_CONTEXT" ]; then
  fail "Active kube context is '$CTX' but EXPECTED_KUBE_CONTEXT='$EXPECTED_KUBE_CONTEXT'."
fi
info "Active kube context : $CTX"
info "Namespace           : $NAMESPACE"
info "Source (current pri): $SOURCE_CLUSTER"
info "Target (to promote) : $TARGET_CLUSTER"

# Confirm we can actually talk to the API server (no localhost-refused surprises).
kubectl version -o yaml >/dev/null 2>&1 || fail "Cannot reach the Kubernetes API server with context '$CTX'."

for c in "$SOURCE_CLUSTER" "$TARGET_CLUSTER"; do
  kubectl -n "$NAMESPACE" get cluster.postgresql.cnpg.io "$c" >/dev/null 2>&1 \
    || fail "CNPG Cluster '$c' not found in namespace '$NAMESPACE'."
done

SRC_PRIMARY="$(kubectl -n "$NAMESPACE" get cluster.postgresql.cnpg.io "$SOURCE_CLUSTER" -o jsonpath='{.status.currentPrimary}' 2>/dev/null || true)"
TGT_PRIMARY="$(kubectl -n "$NAMESPACE" get cluster.postgresql.cnpg.io "$TARGET_CLUSTER" -o jsonpath='{.status.currentPrimary}' 2>/dev/null || true)"
[ -n "$SRC_PRIMARY" ] || fail "Could not resolve current primary pod of $SOURCE_CLUSTER."
[ -n "$TGT_PRIMARY" ] || fail "Could not resolve (designated) primary pod of $TARGET_CLUSTER."
info "Source primary pod  : $SRC_PRIMARY"
info "Target primary pod  : $TGT_PRIMARY (designated primary while in recovery)"

# psql helper: local superuser over the unix socket inside the pod -> no password
# ever appears on a command line or in output. Returns a single scalar on stdout.
# --request-timeout bounds a hung exec; ON_ERROR_STOP makes a failed query exit
# non-zero (callers treat empty output as failure); psql stderr (NOTICE/WARNING)
# is dropped so it can never garble the numeric/boolean result being parsed.
psql_on() { # <pod> <sql>
  kubectl -n "$NAMESPACE" --request-timeout="$KUBECTL_EXEC_TIMEOUT" exec "$1" -c postgres -- \
    psql -U postgres -qtAX -v ON_ERROR_STOP=1 -c "$2" 2>/dev/null
}

# --- precondition 0: -9 must currently be a replica (in recovery) ------------
TGT_IN_RECOVERY="$(psql_on "$TGT_PRIMARY" 'SELECT pg_is_in_recovery();' | tr -d '[:space:]')"
if [ "$TGT_IN_RECOVERY" = "f" ]; then
  ok "$TARGET_CLUSTER is already NOT in recovery -- it appears already promoted."
  info "Nothing to do (idempotent). Verifying and exiting."
  psql_on "$TGT_PRIMARY" 'SELECT pg_is_in_recovery();' >/dev/null
  ok "Promotion already complete for $TARGET_CLUSTER."
  exit 0
fi
[ "$TGT_IN_RECOVERY" = "t" ] || fail "Unexpected pg_is_in_recovery() result on $TARGET_CLUSTER: '$TGT_IN_RECOVERY'."
ok "$TARGET_CLUSTER is in recovery (a replica), as expected."

SRC_IN_RECOVERY="$(psql_on "$SRC_PRIMARY" 'SELECT pg_is_in_recovery();' | tr -d '[:space:]')"
[ "$SRC_IN_RECOVERY" = "f" ] || fail "$SOURCE_CLUSTER primary pod $SRC_PRIMARY is itself in recovery -- refusing."
ok "$SOURCE_CLUSTER is a live primary."

# --- precondition 1: application writes quiesced on -8 -----------------------
info "Checking application writes are quiesced on $SOURCE_CLUSTER ..."
ACTIVE_WRITERS="$(psql_on "$SRC_PRIMARY" \
  "SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend' AND pid <> pg_backend_pid() AND state IS DISTINCT FROM 'idle';" \
  | tr -d '[:space:]')"
info "Active (non-idle) client backends on $SOURCE_CLUSTER: ${ACTIVE_WRITERS:-?}"
if [ "${ACTIVE_WRITERS:-1}" != "0" ]; then
  warn "There are still active client backends on $SOURCE_CLUSTER."
  warn "This script deliberately does NOT scale anything down -- YOU must quiesce all"
  warn "write producers to ZERO *before* running promote (the same quiescing that"
  warn "scripts/cutover-app-to-pg9.sh performs in its 'gate 2'). Run these first:"
  warn "  # 1. web tier"
  warn "  kubectl -n $NAMESPACE scale deployment/web --replicas=0"
  warn "  kubectl -n $NAMESPACE rollout status deployment/web --timeout=300s"
  warn "  # 2. Ray background workers (kuberay recreates pods later)"
  warn "  kubectl -n $NAMESPACE delete pod -l ray.io/cluster=raycluster-kuberay"
  warn "  # 3. ensure no write Jobs are running"
  warn "  kubectl -n $NAMESPACE get jobs | grep -E 'web-migrations|web-actor-launch|web-extralink-prefetch'"
  warn "Then re-run this script. (Re-check with: pg_stat_activity active client backends == 0.)"
  fail "Precondition FAILED: application writes are NOT quiesced on $SOURCE_CLUSTER."
fi
ok "No active client backends on $SOURCE_CLUSTER (writes quiesced)."

# --- precondition 2: replay lag is zero --------------------------------------
info "Checking replay lag ($SOURCE_CLUSTER current LSN vs $TARGET_CLUSTER replay LSN) ..."
SRC_LSN="$(psql_on "$SRC_PRIMARY" 'SELECT pg_current_wal_lsn();' | tr -d '[:space:]')"
TGT_LSN="$(psql_on "$TGT_PRIMARY" 'SELECT pg_last_wal_replay_lsn();' | tr -d '[:space:]')"
[ -n "$SRC_LSN" ] || fail "Could not read pg_current_wal_lsn() on $SOURCE_CLUSTER."
[ -n "$TGT_LSN" ] || fail "Could not read pg_last_wal_replay_lsn() on $TARGET_CLUSTER."
info "Source current WAL LSN : $SRC_LSN"
info "Target replay WAL LSN  : $TGT_LSN"
# lag_bytes = source - target ; must be <= 0 (target caught up or ahead).
LAG_BYTES="$(psql_on "$SRC_PRIMARY" "SELECT pg_wal_lsn_diff('$SRC_LSN','$TGT_LSN');" | tr -d '[:space:]')"
info "Replay lag (bytes)     : ${LAG_BYTES:-?}"
case "${LAG_BYTES:-x}" in
  0|-*) ok "Replay lag is zero (target has replayed up to source LSN)." ;;
  *)    fail "Precondition FAILED: replay lag is ${LAG_BYTES} bytes (> 0). Wait for -9 to catch up." ;;
esac

# --- detect topology ---------------------------------------------------------
REPLICA_PRIMARY="$(kubectl -n "$NAMESPACE" get cluster.postgresql.cnpg.io "$TARGET_CLUSTER" -o jsonpath='{.spec.replica.primary}' 2>/dev/null || true)"
REPLICA_ENABLED="$(kubectl -n "$NAMESPACE" get cluster.postgresql.cnpg.io "$TARGET_CLUSTER" -o jsonpath='{.spec.replica.enabled}' 2>/dev/null || true)"
REPLICA_SOURCE="$(kubectl -n "$NAMESPACE" get cluster.postgresql.cnpg.io "$TARGET_CLUSTER" -o jsonpath='{.spec.replica.source}' 2>/dev/null || true)"
info "spec.replica.primary   : ${REPLICA_PRIMARY:-<unset>}"
info "spec.replica.enabled   : ${REPLICA_ENABLED:-<unset>}"
info "spec.replica.source    : ${REPLICA_SOURCE:-<unset>}"

# SAFETY: confirm -9 actually replicates from the expected source cluster. The
# spec.replica.source names an entry in spec.externalClusters, which is normally
# the source cluster name (fhi-pg-main-8). Promoting a cluster fed from the wrong
# source is an irreversible way to strand data -- refuse unless it matches.
if [ -n "$REPLICA_SOURCE" ]; then
  if [ "$REPLICA_SOURCE" != "$SOURCE_CLUSTER" ]; then
    warn "spec.replica.source='$REPLICA_SOURCE' does NOT equal expected source '$SOURCE_CLUSTER'."
    warn "If the externalCluster is legitimately named differently but still points at"
    warn "$SOURCE_CLUSTER, re-run with SOURCE_CLUSTER='$REPLICA_SOURCE' after verifying."
    fail "SAFETY: refusing to promote -- $TARGET_CLUSTER may be replicating from the wrong source."
  fi
  ok "Replica source '$REPLICA_SOURCE' matches expected source $SOURCE_CLUSTER."
else
  warn "spec.replica.source is unset/empty on $TARGET_CLUSTER -- cannot confirm it replicates"
  warn "from $SOURCE_CLUSTER. Verify the source manually before promoting."
fi

TOPOLOGY=""
if [ -n "$REPLICA_PRIMARY" ]; then
  TOPOLOGY="distributed"
elif [ "$REPLICA_ENABLED" = "true" ]; then
  TOPOLOGY="standalone"
else
  fail "Cannot classify $TARGET_CLUSTER replica topology (spec.replica.primary unset and spec.replica.enabled != true). Inspect the manifest before proceeding."
fi
info "Detected replica topology: $TOPOLOGY"

# --- distributed topology: guide, do NOT auto-run ----------------------------
if [ "$TOPOLOGY" = "distributed" ]; then
  warn "$TARGET_CLUSTER is a DISTRIBUTED-topology replica (spec.replica.primary='$REPLICA_PRIMARY')."
  warn "Promotion here requires the CNPG 1.28 controlled-switchover TOKEN procedure,"
  warn "which must also modify the $SOURCE_CLUSTER manifest and cannot be validated"
  warn "offline. This script will NOT perform it automatically. Documented steps:"
  cat >&2 <<EOF

  ${BLD}CNPG 1.28 distributed-topology controlled switchover${RST}
  1. Demote the current primary ($SOURCE_CLUSTER). In its spec.replica set:
         primary: $TARGET_CLUSTER
         source:  $TARGET_CLUSTER
     CNPG archives the final WAL as .partial and writes a demotion token.
  2. Read the token:
         kubectl -n $NAMESPACE get cluster $SOURCE_CLUSTER -o jsonpath='{.status.demotionToken}'
  3. Promote $TARGET_CLUSTER by setting, SIMULTANEOUSLY, in its spec.replica:
         primary:        $TARGET_CLUSTER
         promotionToken: <token from step 2>
         source:         $SOURCE_CLUSTER
     WARNING: applying primary WITHOUT promotionToken triggers a failover that
     forces a rebuild of the former primary. Apply both together.
  4. Verify: kubectl cnpg status $TARGET_CLUSTER  (expect it as primary)
             and pg_is_in_recovery() == false on $TARGET_CLUSTER.

EOF
  fail "FLAGGED: distributed-topology promotion is a human/token operation -- not auto-run."
fi

# --- confirmation ------------------------------------------------------------
printf '\n%s================ FINAL CONFIRMATION ================%s\n' "$BLD" "$RST"
printf 'About to promote STANDALONE replica cluster %s%s%s to an independent primary\n' "$BLD" "$TARGET_CLUSTER" "$RST"
printf 'by setting spec.replica.enabled=false. This is %sIRREVERSIBLE%s in CNPG 1.28.\n' "$RED" "$RST"
printf 'Context=%s  Namespace=%s\n' "$CTX" "$NAMESPACE"
printf 'Type exactly:  %s%s%s\n> ' "$BLD" "$CONFIRM_PHRASE" "$RST"
read -r REPLY
[ "$REPLY" = "$CONFIRM_PHRASE" ] || fail "Confirmation mismatch. Aborting without changes."

# --- promote (standalone) ----------------------------------------------------
info "Patching $TARGET_CLUSTER: spec.replica.enabled=false ..."
kubectl -n "$NAMESPACE" patch cluster.postgresql.cnpg.io "$TARGET_CLUSTER" \
  --type=merge -p '{"spec":{"replica":{"enabled":false}}}' \
  || fail "kubectl patch failed."
ok "Patch applied. Waiting up to ${PROMOTE_TIMEOUT_SECS}s for $TARGET_CLUSTER to leave recovery ..."

# --- verify ------------------------------------------------------------------
SECONDS=0
while :; do
  cur="$(psql_on "$TGT_PRIMARY" 'SELECT pg_is_in_recovery();' 2>/dev/null | tr -d '[:space:]' || true)"
  if [ "$cur" = "f" ]; then
    ok "pg_is_in_recovery() == false on $TARGET_CLUSTER."
    break
  fi
  if [ "$SECONDS" -ge "$PROMOTE_TIMEOUT_SECS" ]; then
    fail "Timed out waiting for $TARGET_CLUSTER to leave recovery (still '$cur')."
  fi
  info "  still in recovery ('$cur'); re-checking ..."
  sleep 5
done

if kubectl cnpg version >/dev/null 2>&1; then
  info "cnpg plugin status for $TARGET_CLUSTER:"
  kubectl cnpg status "$TARGET_CLUSTER" -n "$NAMESPACE" 2>/dev/null | sed -n '1,20p' || true
fi

printf '\n%s================== RESULT: PASS ==================%s\n' "$GRN" "$RST"
ok "$TARGET_CLUSTER promoted to primary and out of recovery."
info "Next: run scripts/cutover-app-to-pg9.sh to point the application at ${TARGET_CLUSTER}-rw."
