# /metrics is cluster-internal

`django_prometheus` mounts `/metrics` at the URL root, and every Ingress host
routes `/` to the web Service, so the endpoint used to answer the public
internet on every domain (including `monitor.fighthealthinsurance.com`, which
was added in July 2026 to scrape it from outside). That exposed per-view
request volumes and latencies, DB connection-pool stats, and ML backend call
counters and failure reasons to anyone who asked.

It is now closed to everything outside the cluster, in two layers.

## 1. Ingress deny (request never reaches the pods)

`Ingress/fight-health-insurance-metrics-deny` in `k8s/deploy.yaml` (and the
dev/staging equivalents) adds an `Exact` `/metrics` rule for each host with
`nginx.ingress.kubernetes.io/whitelist-source-range: 127.0.0.1/32` — an
allowlist nothing can match, so ingress-nginx answers 403 itself. The Exact
path wins over the `/` Prefix rule on the same host; TLS and the rest of the
annotations stay on the main Ingress, which ingress-nginx merges into the same
server block.

On ingress-nginx >= 1.12 the annotation is also spelled
`allowlist-source-range`; the old name is still honored.

## 2. `MetricsAccessMiddleware` (defense in depth, in the app)

Runs first in `MIDDLEWARE` and 404s `/metrics` unless **both** hold:

- `REMOTE_ADDR` (the peer socket address — not spoofable by the client) is in
  `settings.METRICS_ALLOWED_CIDRS`, which defaults to loopback + RFC1918 +
  IPv6 ULA, i.e. what pod and node IPs are. Override with a comma-separated
  `METRICS_ALLOWED_CIDRS` env var if the cluster uses public pod CIDRs.
- No proxy-forwarding header (`X-Forwarded-For`, `X-Real-IP`,
  `X-Forwarded-Host`, `Forwarded`) is present. The ingress always stamps these
  and a client cannot strip them, so their presence means the request came
  from outside — without this check an ingress-forwarded request would pass,
  since the ingress-controller pod IP is itself private.

A direct in-cluster scrape of `http://<pod-ip>:80/metrics` sends none of those
headers and comes from a pod IP, so it is served normally.

## Scraping it now

Use `k8s/fhi-web-podmonitor.yaml` (Prometheus operator, `PodMonitor/fhi-web`),
which scrapes each web pod on its named `web` port. This is also the only
correct way to collect these numbers: each replica keeps its own registry (one
uvicorn worker per pod, no multiprocess dir), so the old ingress scrape
returned whichever pod the session-affinity cookie landed on — a random sixth
of the traffic — instead of the fleet.

Apply it and confirm the operator actually selects it (its
`podMonitorSelector` / `podMonitorNamespaceSelector` must match):

```bash
kubectl -n totallylegitco apply -f k8s/fhi-web-podmonitor.yaml
kubectl -n totallylegitco get podmonitor fhi-web
# Targets should show 6 web pods, state UP, in the Prometheus targets page.
```

Ad-hoc check from inside the cluster:

```bash
kubectl -n totallylegitco exec deploy/web -- curl -s localhost/metrics | head
```

And from outside, both layers should refuse:

```bash
curl -so /dev/null -w '%{http_code}\n' https://monitor.fighthealthinsurance.com/metrics  # 403
```

`monitor.fighthealthinsurance.com` no longer has a purpose of its own — it was
added only to reach `/metrics`. It is left in place (it still serves the site
like any other host) so removing it can be a separate, deliberate change to
the TLS host list.
