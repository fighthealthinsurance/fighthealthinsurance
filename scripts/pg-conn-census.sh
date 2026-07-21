#!/usr/bin/env bash
# pg-conn-census.sh -- who is holding Postgres connections on fhi-pg-main-9?
#
# Prints, from the current primary: connection counts per client/state, the
# last query of long-idle (likely stranded) connections, and the total vs
# max_connections. The long-idle "last query" fingerprints which code path a
# leaked connection came from -- this is how the chooser/banner leaks were
# attributed during the July 2026 connection-starvation incident.
#
# Usage: ./scripts/pg-conn-census.sh [minutes-idle-threshold]   (default 30)
set -euo pipefail

NS=totallylegitco
THRESHOLD="${1:-30}"
case "$THRESHOLD" in
  '' | *[!0-9]*)
    echo "usage: $0 [minutes-idle-threshold]  (integer minutes)" >&2
    exit 1
    ;;
esac
PRIMARY="$(kubectl -n "$NS" get cluster fhi-pg-main-9 -o jsonpath='{.status.currentPrimary}')"

run_sql() {
  kubectl -n "$NS" exec "$PRIMARY" -c postgres -- psql -U postgres -d app -Atc "$1"
}

echo "== connections by client/state (primary: $PRIMARY) =="
run_sql "SELECT coalesce(client_addr::text,'local'), state, count(*),
                date_trunc('second', max(now()-state_change))::text AS oldest
         FROM pg_stat_activity WHERE usename='ziggystardust'
         GROUP BY 1,2 ORDER BY 1,2;"
echo
echo "== last query of connections idle > ${THRESHOLD} min (leak fingerprints) =="
run_sql "SELECT coalesce(client_addr::text,'local'),
                date_trunc('second', now()-state_change)::text AS idle_for, left(query,90)
         FROM pg_stat_activity
         WHERE usename='ziggystardust' AND state='idle'
           AND now()-state_change > interval '${THRESHOLD} minutes'
         ORDER BY 1, now()-state_change DESC;"
echo
echo "== total backends vs max_connections =="
run_sql "SELECT count(*) || ' / ' || current_setting('max_connections') FROM pg_stat_activity;"
