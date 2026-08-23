# /metrics is cluster-internal (plus UptimeRobot)

`django_prometheus` mounts `/metrics` at the URL root, and every Ingress host
routes `/` to the web Service, so the endpoint used to answer the public
internet on every domain (including `monitor.fighthealthinsurance.com`, which
was added in July 2026 to scrape it from outside). That exposed per-view
request volumes and latencies, DB connection-pool stats, and ML backend call
counters and failure reasons to anyone who asked.

It is now closed to everything outside the cluster except UptimeRobot's
probes, in two layers.

## 1. Ingress deny (request never reaches the pods)

`Ingress/fight-health-insurance-metrics-deny` in `k8s/deploy.yaml` (and the
dev/staging equivalents) adds an `Exact` `/metrics` rule for each host with
`nginx.ingress.kubernetes.io/whitelist-source-range: 127.0.0.1/32` — an
allowlist nothing can match, so ingress-nginx answers 403 itself. The Exact
path wins over the `/` Prefix rule on the same host; TLS and the rest of the
annotations stay on the main Ingress, which ingress-nginx merges into the same
server block.

`monitor.fighthealthinsurance.com` is deliberately **not** in that deny list —
it is the UptimeRobot door, and the app decides who walks through it (below).
That is also what the host is now for; it had no other purpose after this
change.

On ingress-nginx >= 1.12 the annotation is also spelled
`allowlist-source-range`; the old name is still honored.

## 2. `MetricsAccessMiddleware` (the app's own gate)

Runs first in `MIDDLEWARE`. For `/metrics` it serves exactly two kinds of
caller and 404s everything else:

**In-cluster, direct.** `REMOTE_ADDR` (the peer socket address — not spoofable
by the client) is in `settings.METRICS_ALLOWED_CIDRS`, which defaults to
loopback + RFC1918 + IPv6 ULA, i.e. what pod and node IPs are; **and** no
proxy-forwarding header (`X-Forwarded-For`, `X-Real-IP`, `X-Forwarded-Host`,
`Forwarded`) is present. The ingress always stamps those and a client cannot
strip them, so their presence means the request came from outside — without
that check an ingress-forwarded request would pass, since the
ingress-controller pod IP is itself private. Override the ranges with a
comma-separated `METRICS_ALLOWED_CIDRS` env var if the cluster uses public pod
CIDRs.

**A known external monitor.** A forwarded request is served when the client
address *the ingress observed* is one of UptimeRobot's published probe
addresses (`fighthealthinsurance/uptimerobot_ips.py`) or is listed in
`METRICS_ALLOWED_FORWARDED_CIDRS` (comma-separated env var, empty by default).
That address is read from `X-Real-IP`, which ingress-nginx overwrites on every
request; the fallback is the **last** `X-Forwarded-For` entry, the one the
ingress itself appended — earlier entries are whatever the client sent and are
ignored.

Worth knowing: this means anything able to originate a request from an
UptimeRobot address can read the metric set. If that ever matters more than
the convenience, point the UptimeRobot check at `/ziggy/rest/ping` (public, and
what the k8s probes already use) and drop the monitor host from
`ALLOWED_HOSTS`, or swap the IP allowlist for a secret header UptimeRobot
sends.

### Refreshing the UptimeRobot list

UptimeRobot rotates probe addresses. When probes start 404ing:

```bash
python scripts/update_uptimerobot_ips.py   # rewrites fighthealthinsurance/uptimerobot_ips.py
git commit -am "Refresh UptimeRobot probe addresses"
```

The list lives only in that generated module — deliberately not duplicated
into the ingress annotation — so this one command is the whole refresh.

## Scraping it now

Use `k8s/fhi-web-podmonitor.yaml` (Prometheus operator, `PodMonitor/fhi-web`),
which scrapes each web pod on its named `web` port; `scripts/build.sh` applies
it on deploy. This is also the only correct way to collect these numbers: each
replica keeps its own registry (one uvicorn worker per pod, no multiprocess
dir), so the old ingress scrape returned whichever pod the session-affinity
cookie landed on — a random sixth of the traffic — instead of the fleet.

Because the scrape addresses a pod by IP, the request's `Host` header is that
pod IP — which prod's `ALLOWED_HOSTS` would reject with a 400 before the
metrics view ever ran. The web Deployment therefore injects `POD_IP` from the
downward API and `settings.py` appends it to `ALLOWED_HOSTS`. Keep those two
together: dropping either turns every scrape into a `DisallowedHost`.

Confirm the operator actually selects it (its `podMonitorSelector` /
`podMonitorNamespaceSelector` must match):

```bash
kubectl -n totallylegitco get podmonitor fhi-web
# Targets should show 6 web pods, state UP, in the Prometheus targets page.
```

Ad-hoc check from inside the cluster (addressed by pod IP for the same
`ALLOWED_HOSTS` reason — `curl localhost/metrics` sends `Host: localhost` and
gets a 400):

```bash
kubectl -n totallylegitco exec deploy/web -- sh -c 'curl -s "http://$POD_IP/metrics" | head'
```

From outside, both layers should refuse — the first at the edge, the second in
the app:

```bash
curl -so /dev/null -w '%{http_code}\n' https://www.fighthealthinsurance.com/metrics      # 403
curl -so /dev/null -w '%{http_code}\n' https://monitor.fighthealthinsurance.com/metrics  # 404
```
