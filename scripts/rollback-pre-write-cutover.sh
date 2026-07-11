#!/bin/bash
#
# rollback-pre-write-cutover.sh -- Revert the application DB host back to
# fhi-pg-main-8-rw. This is the TRIVIAL rollback that is ONLY safe *before* the
# application has written anything to fhi-pg-main-9.
#
#   * If the app is still pointed at -8 (no cutover yet): a no-op-ish safety
#     restore -- confirm -8 is the host and -9 is idle.
#   * If the app was cut over to -9 but has NOT taken writes yet: flip the host
#     back to -8 and roll the app.
#
# ############################################################################
# ## POST-WRITE CASE -- READ THIS.                                           ##
# ## Once the application has COMMITTED writes to fhi-pg-main-9, pointing the ##
# ## app back at fhi-pg-main-8 SILENTLY LOSES those newest writes: -8 stopped ##
# ## receiving data at promotion time and (in a standalone promotion) is now  ##
# ## a divergent, independent cluster. This script DETECTS likely writes on   ##
# ## -9 and REFUSES. Recovering to -8 after real writes requires one of:      ##
# ##   - reverse replication -9 -> -8 then a second controlled switchover, or ##
# ##   - a restore of -9's newest data into -8, or                            ##
# ##   - an explicit business decision to ACCEPT the data loss.               ##
# ## All three are OUT OF SCOPE for this script.                              ##
# ############################################################################
#
# Env overrides:
#   NAMESPACE           (default totallylegitco)
#   CONFIGMAP           (default fhi-db-config)
#   CONFIGMAP_KEY       (default PDBHOST)
#   ROLLBACK_HOST       (default fhi-pg-main-8-rw.${NAMESPACE}.svc)
#   TARGET_CLUSTER      (default fhi-pg-main-9)  the cluster we are rolling AWAY from
#   WRITE_DEPLOYMENTS   (default "web")
#   WEB_TARGET_REPLICAS (default 3)
#   REVERSE_PATCH_DIR   (default ./.pg9-cutover-state)
#   ROLLOUT_TIMEOUT     (default 300s)
#
set -euo pipefail

NAMESPACE="${NAMESPACE:-totallylegitco}"
CONFIGMAP="${CONFIGMAP:-fhi-db-config}"
CONFIGMAP_KEY="${CONFIGMAP_KEY:-PDBHOST}"
ROLLBACK_HOST="${ROLLBACK_HOST:-fhi-pg-main-8-rw.${NAMESPACE}.svc}"
TARGET_CLUSTER="${TARGET_CLUSTER:-fhi-pg-main-9}"
WRITE_DEPLOYMENTS="${WRITE_DEPLOYMENTS:-web}"
WEB_TARGET_REPLICAS="${WEB_TARGET_REPLICAS:-3}"
REVERSE_PATCH_DIR="${REVERSE_PATCH_DIR:-./.pg9-cutover-state}"
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-300s}"
ACK_PHRASE="no writes reached ${TARGET_CLUSTER}"

RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; BLD=$'\033[1m'; RST=$'\033[0m'
info() { printf '%s[rollback]%s %s\n' "$BLD" "$RST" "$*"; }
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
kubectl -n "$NAMESPACE" get configmap "$CONFIGMAP" >/dev/null 2>&1 || fail "ConfigMap $CONFIGMAP not found."
info "Context=$CTX  Namespace=$NAMESPACE  rollback host=$ROLLBACK_HOST"

CUR_HOST="$(kubectl -n "$NAMESPACE" get configmap "$CONFIGMAP" -o jsonpath="{.data.$CONFIGMAP_KEY}" 2>/dev/null || true)"
[ -n "$CUR_HOST" ] || fail "ConfigMap $CONFIGMAP has no key $CONFIGMAP_KEY."
info "Current $CONFIGMAP_KEY = $CUR_HOST"

psql_on() { kubectl -n "$NAMESPACE" exec "$1" -c postgres -- psql -U postgres -qtAX -c "$2"; }

TGT_PRIMARY="$(kubectl -n "$NAMESPACE" get cluster.postgresql.cnpg.io "$TARGET_CLUSTER" -o jsonpath='{.status.currentPrimary}' 2>/dev/null || true)"

# --- already on -8? trivial safe state --------------------------------------
if [ "$CUR_HOST" = "$ROLLBACK_HOST" ]; then
  ok "Application is already pointed at $ROLLBACK_HOST -- no host flip needed."
  if [ -n "$TGT_PRIMARY" ]; then
    n="$(psql_on "$TGT_PRIMARY" "SELECT count(*) FROM pg_stat_activity WHERE backend_type='client backend' AND pid<>pg_backend_pid() AND state IS DISTINCT FROM 'idle';" | tr -d '[:space:]' || echo '?')"
    info "Active client backends on $TARGET_CLUSTER: ${n}"
    if [ "${n:-1}" = "0" ]; then
      ok "$TARGET_CLUSTER is not taking writes."
    else
      warn "$TARGET_CLUSTER has active client backends despite app being on -8 -- investigate."
    fi
  fi
  info "Nothing to roll back."
  exit 0
fi

# --- we are post-cutover (host points at -9): assess write risk --------------
warn "ConfigMap host is '$CUR_HOST' (NOT $ROLLBACK_HOST) -- the app was cut over."
if [ -n "$TGT_PRIMARY" ]; then
  IN_REC="$(psql_on "$TGT_PRIMARY" 'SELECT pg_is_in_recovery();' | tr -d '[:space:]' || echo '?')"
  ACTIVE="$(psql_on "$TGT_PRIMARY" "SELECT count(*) FROM pg_stat_activity WHERE backend_type='client backend' AND pid<>pg_backend_pid() AND state IS DISTINCT FROM 'idle';" | tr -d '[:space:]' || echo '?')"
  info "$TARGET_CLUSTER pg_is_in_recovery = ${IN_REC}; active client backends = ${ACTIVE}"
  if [ "${ACTIVE:-1}" != "0" ]; then
    printf '\n%s############################ REFUSING ############################%s\n' "$RED" "$RST"
    warn "$TARGET_CLUSTER has ACTIVE client backends right now -- the application is"
    warn "live on -9 and is very likely writing. Rolling the host back to -8 now would"
    warn "LOSE those writes. This is the POST-WRITE case."
    warn "Do NOT proceed. Options (all OUT OF SCOPE for this script):"
    warn "  1) reverse-replicate -9 -> -8, then controlled switchover back; or"
    warn "  2) restore -9's newest data into -8; or"
    warn "  3) make an explicit decision to ACCEPT data loss, then re-run with the app"
    warn "     scaled to 0 and after you understand exactly what is lost."
    fail "Post-write rollback is not safe to automate. Aborting."
  fi
  warn "No ACTIVE backends on -9 right now, but writes may have occurred earlier."
fi

printf '\n%s================ PRE-WRITE ROLLBACK GATE ================%s\n' "$BLD" "$RST"
warn "This ONLY safe if the application has committed NO writes to $TARGET_CLUSTER."
warn "If ANY writes reached $TARGET_CLUSTER, STOP and use reverse-replication / restore /"
warn "accept-loss instead (see the header of this script)."
printf 'To proceed you assert no writes reached -9. Type exactly:\n  %s%s%s\n> ' "$BLD" "$ACK_PHRASE" "$RST"
read -r REPLY
[ "$REPLY" = "$ACK_PHRASE" ] || fail "Acknowledgement mismatch. Aborting (treating as unsafe)."

# --- scale writers to zero before flipping back -----------------------------
declare -A ORIG_REPLICAS
for dep in $WRITE_DEPLOYMENTS; do
  kubectl -n "$NAMESPACE" get deployment "$dep" >/dev/null 2>&1 || { warn "Deployment $dep not found; skipping."; continue; }
  cur="$(kubectl -n "$NAMESPACE" get deployment "$dep" -o jsonpath='{.spec.replicas}' 2>/dev/null || echo 0)"
  ORIG_REPLICAS["$dep"]="$cur"
  if [ "${cur:-0}" != "0" ]; then
    info "Scaling $dep -> 0 before flip ..."
    kubectl -n "$NAMESPACE" scale deployment "$dep" --replicas=0
    kubectl -n "$NAMESPACE" rollout status deployment "$dep" --timeout="$ROLLOUT_TIMEOUT" || true
  fi
done

# --- apply reverse patch (prefer the one cutover saved) ----------------------
REVERSE_PATCH="$REVERSE_PATCH_DIR/reverse-configmap-patch.json"
if [ -f "$REVERSE_PATCH" ]; then
  info "Applying saved reverse patch $REVERSE_PATCH ..."
  kubectl -n "$NAMESPACE" patch configmap "$CONFIGMAP" --type=merge -p "$(cat "$REVERSE_PATCH")" || fail "Reverse patch failed."
else
  warn "Saved reverse patch not found; falling back to ROLLBACK_HOST=$ROLLBACK_HOST."
  kubectl -n "$NAMESPACE" patch configmap "$CONFIGMAP" --type=merge \
    -p "{\"data\":{\"$CONFIGMAP_KEY\":\"$ROLLBACK_HOST\"}}" || fail "Reverse patch failed."
fi
APPLIED="$(kubectl -n "$NAMESPACE" get configmap "$CONFIGMAP" -o jsonpath="{.data.$CONFIGMAP_KEY}")"
info "ConfigMap now $CONFIGMAP_KEY=$APPLIED"
case "$APPLIED" in
  fhi-pg-main-8-rw*) ok "Host restored to the -8 primary." ;;
  *) warn "Host is '$APPLIED' -- expected an fhi-pg-main-8-rw endpoint. Verify manually." ;;
esac

# --- bring app back on -8 ----------------------------------------------------
for dep in $WRITE_DEPLOYMENTS; do
  kubectl -n "$NAMESPACE" get deployment "$dep" >/dev/null 2>&1 || continue
  want="${ORIG_REPLICAS[$dep]:-0}"
  [ "${want:-0}" = "0" ] && want="$WEB_TARGET_REPLICAS"
  info "Scaling $dep -> $want (fresh pods reconnect to $APPLIED) ..."
  kubectl -n "$NAMESPACE" scale deployment "$dep" --replicas="$want"
  kubectl -n "$NAMESPACE" rollout status deployment "$dep" --timeout="$ROLLOUT_TIMEOUT" || fail "Rollout of $dep did not complete."
  ok "$dep rolled out at $want replicas."
done

# --- verify -9 not taking writes --------------------------------------------
if [ -n "$TGT_PRIMARY" ]; then
  sleep 5
  n="$(psql_on "$TGT_PRIMARY" "SELECT count(*) FROM pg_stat_activity WHERE backend_type='client backend' AND pid<>pg_backend_pid() AND state IS DISTINCT FROM 'idle';" | tr -d '[:space:]' || echo '?')"
  info "Active client backends on $TARGET_CLUSTER after rollback: ${n}"
  if [ "${n:-1}" = "0" ]; then
    ok "$TARGET_CLUSTER is no longer taking writes."
  else
    warn "$TARGET_CLUSTER still shows active backends -- investigate before declaring success."
  fi
fi

printf '\n%s================== ROLLBACK: PASS ==================%s\n' "$GRN" "$RST"
ok "Application reverted to $APPLIED."
