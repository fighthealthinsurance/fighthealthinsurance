#!/bin/bash
#
# cutover-app-to-pg9.sh -- Point the application at the promoted fhi-pg-main-9
# primary by flipping the single configurable DB host (fhi-db-config/PDBHOST)
# from fhi-pg-main-8-rw to fhi-pg-main-9-rw, then rolling the app.
#
# ORDER OF SAFETY (never allow writes to BOTH clusters):
#   1. Refuse unless -9 promotion is verified (pg_is_in_recovery == false on -9).
#   2. Scale the write-producing app workloads to ZERO (so nothing writes during
#      the flip) and confirm -8 has no active client backends.
#   3. Save the REVERSE patch (current PDBHOST) to disk.
#   4. Patch fhi-db-config/PDBHOST -> fhi-pg-main-9-rw.
#   5. Bring the app back (fresh pods read the new host) and restart Ray.
#   6. Smoke-check: app backends now appear on -9, and NO app backends appear on -8.
#
# Reversible: the reverse patch file (and scripts/rollback-pre-write-cutover.sh)
# points PDBHOST back at -8. That is only safe BEFORE the app has written to -9;
# see rollback-pre-write-cutover.sh for the post-write caveat.
#
# Env overrides:
#   NAMESPACE            (default totallylegitco)
#   CONFIGMAP            (default fhi-db-config)
#   CONFIGMAP_KEY        (default PDBHOST)
#   TARGET_CLUSTER       (default fhi-pg-main-9)
#   NEW_DB_HOST          (default ${TARGET_CLUSTER}-rw.${NAMESPACE}.svc)
#   WRITE_DEPLOYMENTS    (default "web")  space-separated Deployments to cycle
#   WEB_TARGET_REPLICAS  (default 3) replicas to restore if a deployment was at 0
#   RAYCLUSTER           (default raycluster-kuberay; empty to skip Ray restart)
#   REVERSE_PATCH_DIR    (default ./.pg9-cutover-state)
#   ROLLOUT_TIMEOUT      (default 300s)
#
set -euo pipefail

NAMESPACE="${NAMESPACE:-totallylegitco}"
CONFIGMAP="${CONFIGMAP:-fhi-db-config}"
CONFIGMAP_KEY="${CONFIGMAP_KEY:-PDBHOST}"
TARGET_CLUSTER="${TARGET_CLUSTER:-fhi-pg-main-9}"
NEW_DB_HOST="${NEW_DB_HOST:-${TARGET_CLUSTER}-rw.${NAMESPACE}.svc}"
WRITE_DEPLOYMENTS="${WRITE_DEPLOYMENTS:-web}"
WEB_TARGET_REPLICAS="${WEB_TARGET_REPLICAS:-3}"
RAYCLUSTER="${RAYCLUSTER:-raycluster-kuberay}"
REVERSE_PATCH_DIR="${REVERSE_PATCH_DIR:-./.pg9-cutover-state}"
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-300s}"
CONFIRM_PHRASE="cutover to ${NEW_DB_HOST}"

RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; BLD=$'\033[1m'; RST=$'\033[0m'
info() { printf '%s[cutover]%s %s\n' "$BLD" "$RST" "$*"; }
ok()   { printf '%s[ PASS ]%s %s\n' "$GRN" "$RST" "$*"; }
warn() { printf '%s[ WARN ]%s %s\n' "$YEL" "$RST" "$*" >&2; }
fail() { printf '%s[ FAIL ]%s %s\n' "$RED" "$RST" "$*" >&2; exit 1; }

# --- preflight ---------------------------------------------------------------
command -v kubectl >/dev/null 2>&1 || fail "kubectl not found on PATH."
CTX="$(kubectl config current-context 2>/dev/null || true)"
[ -n "$CTX" ] || fail "No active kube context."
if [ -n "${EXPECTED_KUBE_CONTEXT:-}" ] && [ "$CTX" != "$EXPECTED_KUBE_CONTEXT" ]; then
  fail "Active context '$CTX' != EXPECTED_KUBE_CONTEXT '$EXPECTED_KUBE_CONTEXT'."
fi
kubectl version -o yaml >/dev/null 2>&1 || fail "Cannot reach the Kubernetes API server."
info "Context=$CTX  Namespace=$NAMESPACE  ConfigMap=$CONFIGMAP/$CONFIGMAP_KEY"
info "New DB host = $NEW_DB_HOST"

kubectl -n "$NAMESPACE" get configmap "$CONFIGMAP" >/dev/null 2>&1 \
  || fail "ConfigMap $CONFIGMAP not found (apply k8s/db-config.yaml first)."
kubectl -n "$NAMESPACE" get cluster.postgresql.cnpg.io "$TARGET_CLUSTER" >/dev/null 2>&1 \
  || fail "CNPG Cluster $TARGET_CLUSTER not found."

CUR_HOST="$(kubectl -n "$NAMESPACE" get configmap "$CONFIGMAP" -o jsonpath="{.data.$CONFIGMAP_KEY}" 2>/dev/null || true)"
[ -n "$CUR_HOST" ] || fail "ConfigMap $CONFIGMAP has no key $CONFIGMAP_KEY."
info "Current $CONFIGMAP_KEY = $CUR_HOST"

psql_on() { kubectl -n "$NAMESPACE" exec "$1" -c postgres -- psql -U postgres -qtAX -c "$2"; }

# --- gate 1: -9 promotion verified ------------------------------------------
TGT_PRIMARY="$(kubectl -n "$NAMESPACE" get cluster.postgresql.cnpg.io "$TARGET_CLUSTER" -o jsonpath='{.status.currentPrimary}' 2>/dev/null || true)"
[ -n "$TGT_PRIMARY" ] || fail "Could not resolve primary pod of $TARGET_CLUSTER."
IN_REC="$(psql_on "$TGT_PRIMARY" 'SELECT pg_is_in_recovery();' | tr -d '[:space:]')"
[ "$IN_REC" = "f" ] || fail "$TARGET_CLUSTER is still in recovery (pg_is_in_recovery='$IN_REC'). Run scripts/promote-pg9.sh first."
ok "$TARGET_CLUSTER promotion verified (not in recovery)."

# Idempotency: already cut over?
if [ "$CUR_HOST" = "$NEW_DB_HOST" ]; then
  ok "ConfigMap already points at $NEW_DB_HOST -- cutover already applied. Nothing to change."
  info "If pods predate the change, restart them manually: kubectl -n $NAMESPACE rollout restart deployment/<name>"
  exit 0
fi

# --- confirmation ------------------------------------------------------------
printf '\n%s================ CUTOVER CONFIRMATION ================%s\n' "$BLD" "$RST"
printf 'Repoint the application DB host:\n  FROM %s%s%s\n  TO   %s%s%s\n' "$YEL" "$CUR_HOST" "$RST" "$GRN" "$NEW_DB_HOST" "$RST"
printf 'This scales %s to 0, flips %s/%s, then restores the app.\n' "$WRITE_DEPLOYMENTS" "$CONFIGMAP" "$CONFIGMAP_KEY"
printf 'Type exactly:  %s%s%s\n> ' "$BLD" "$CONFIRM_PHRASE" "$RST"
read -r REPLY
[ "$REPLY" = "$CONFIRM_PHRASE" ] || fail "Confirmation mismatch. Aborting without changes."

# --- gate 2: quiesce writers to zero ----------------------------------------
mkdir -p "$REVERSE_PATCH_DIR"
declare -A ORIG_REPLICAS
for dep in $WRITE_DEPLOYMENTS; do
  if ! kubectl -n "$NAMESPACE" get deployment "$dep" >/dev/null 2>&1; then
    warn "Deployment '$dep' not found; skipping."
    continue
  fi
  cur="$(kubectl -n "$NAMESPACE" get deployment "$dep" -o jsonpath='{.spec.replicas}' 2>/dev/null || echo 0)"
  ORIG_REPLICAS["$dep"]="$cur"
  info "Deployment $dep currently at ${cur} replicas."
  if [ "${cur:-0}" != "0" ]; then
    info "Scaling $dep -> 0 to quiesce writes during the flip ..."
    kubectl -n "$NAMESPACE" scale deployment "$dep" --replicas=0
    kubectl -n "$NAMESPACE" rollout status deployment "$dep" --timeout="$ROLLOUT_TIMEOUT" || true
  fi
done

# Stop Ray write-producers too (kuberay recreates pods after cutover).
if [ -n "$RAYCLUSTER" ] && kubectl -n "$NAMESPACE" get raycluster "$RAYCLUSTER" >/dev/null 2>&1; then
  info "Deleting Ray pods for $RAYCLUSTER so they drop their -8 connections (operator recreates them post-flip)."
  kubectl -n "$NAMESPACE" delete pod -l "ray.io/cluster=$RAYCLUSTER" --wait=false 2>/dev/null \
    || warn "Could not delete Ray pods by label ray.io/cluster=$RAYCLUSTER (check the label)."
else
  [ -n "$RAYCLUSTER" ] && warn "RayCluster $RAYCLUSTER not found; skipping Ray restart (do it manually if it exists under another name)."
fi

# Confirm -8 has no live app backends before flipping (never write to both).
SRC_HOST_SHORT="${CUR_HOST%%.*}"                     # e.g. fhi-pg-main-8-rw
SRC_CLUSTER="${SRC_HOST_SHORT%-rw}"                  # e.g. fhi-pg-main-8
SRC_PRIMARY="$(kubectl -n "$NAMESPACE" get cluster.postgresql.cnpg.io "$SRC_CLUSTER" -o jsonpath='{.status.currentPrimary}' 2>/dev/null || true)"
if [ -n "$SRC_PRIMARY" ]; then
  n="$(psql_on "$SRC_PRIMARY" "SELECT count(*) FROM pg_stat_activity WHERE backend_type='client backend' AND pid<>pg_backend_pid() AND state IS DISTINCT FROM 'idle';" | tr -d '[:space:]' || echo '?')"
  info "Active client backends still on $SRC_CLUSTER: ${n}"
  [ "${n:-1}" = "0" ] || fail "Source $SRC_CLUSTER still has active client backends -- refusing to flip (would risk writes to both)."
  ok "Source $SRC_CLUSTER has no active client backends."
else
  warn "Could not resolve source cluster '$SRC_CLUSTER' primary to verify quiesce; proceeding on scale-to-zero guarantee."
fi

# --- save reverse patch ------------------------------------------------------
REVERSE_PATCH="$REVERSE_PATCH_DIR/reverse-configmap-patch.json"
printf '{"data":{"%s":"%s"}}\n' "$CONFIGMAP_KEY" "$CUR_HOST" > "$REVERSE_PATCH"
ok "Reverse patch saved: $REVERSE_PATCH  (restores $CONFIGMAP_KEY=$CUR_HOST)"

# --- flip --------------------------------------------------------------------
info "Patching $CONFIGMAP/$CONFIGMAP_KEY -> $NEW_DB_HOST ..."
kubectl -n "$NAMESPACE" patch configmap "$CONFIGMAP" --type=merge \
  -p "{\"data\":{\"$CONFIGMAP_KEY\":\"$NEW_DB_HOST\"}}" || fail "ConfigMap patch failed."
APPLIED="$(kubectl -n "$NAMESPACE" get configmap "$CONFIGMAP" -o jsonpath="{.data.$CONFIGMAP_KEY}")"
[ "$APPLIED" = "$NEW_DB_HOST" ] || fail "ConfigMap did not take the new value (got '$APPLIED')."
ok "ConfigMap now $CONFIGMAP_KEY=$APPLIED"

# --- bring the app back ------------------------------------------------------
for dep in $WRITE_DEPLOYMENTS; do
  kubectl -n "$NAMESPACE" get deployment "$dep" >/dev/null 2>&1 || continue
  want="${ORIG_REPLICAS[$dep]:-0}"
  [ "${want:-0}" = "0" ] && want="$WEB_TARGET_REPLICAS"
  info "Scaling $dep -> $want (fresh pods pick up $CONFIGMAP_KEY=$NEW_DB_HOST) ..."
  kubectl -n "$NAMESPACE" scale deployment "$dep" --replicas="$want"
  kubectl -n "$NAMESPACE" rollout status deployment "$dep" --timeout="$ROLLOUT_TIMEOUT" \
    || fail "Rollout of $dep did not complete."
  ok "$dep rolled out at $want replicas."
done

# --- smoke checks ------------------------------------------------------------
info "Smoke checks ..."
sleep 5
NEW_BACKENDS="$(psql_on "$TGT_PRIMARY" "SELECT count(*) FROM pg_stat_activity WHERE backend_type='client backend' AND pid<>pg_backend_pid();" | tr -d '[:space:]' || echo '?')"
info "Client backends on $TARGET_CLUSTER (new primary): ${NEW_BACKENDS}"
if [ "${NEW_BACKENDS:-0}" -ge 1 ] 2>/dev/null; then
  ok "Application is connecting to $TARGET_CLUSTER."
else
  warn "No client backends observed on $TARGET_CLUSTER yet -- pods may still be warming up. Re-check pg_stat_activity."
fi
if [ -n "$SRC_PRIMARY" ]; then
  STILL="$(psql_on "$SRC_PRIMARY" "SELECT count(*) FROM pg_stat_activity WHERE backend_type='client backend' AND pid<>pg_backend_pid() AND state IS DISTINCT FROM 'idle';" | tr -d '[:space:]' || echo '?')"
  info "Active client backends still on $SRC_CLUSTER: ${STILL}"
  if [ "${STILL:-1}" = "0" ]; then
    ok "No active app writes on $SRC_CLUSTER (single-writer invariant holds)."
  else
    warn "Unexpected active backends on $SRC_CLUSTER -- investigate immediately (possible dual-writer)."
  fi
fi

printf '\n%s================== CUTOVER: PASS ==================%s\n' "$GRN" "$RST"
ok "Application DB host is now $NEW_DB_HOST."
info "Reverse (pre-write only): scripts/rollback-pre-write-cutover.sh  (uses $REVERSE_PATCH)"
