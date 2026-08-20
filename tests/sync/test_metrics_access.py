"""The Prometheus /metrics endpoint answers in-cluster callers and UptimeRobot.

Covers both layers of the lockdown: MetricsAccessMiddleware in the app, and
the deny Ingress that keeps the request from reaching the pods at all.
"""

from ipaddress import ip_address
from pathlib import Path

import yaml
from django.http import HttpResponse
from django.test import RequestFactory, TestCase, override_settings

from fighthealthinsurance.middleware import MetricsAccessMiddleware
from fighthealthinsurance.uptimerobot_ips import UPTIMEROBOT_IPS

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEPLOY_MANIFESTS = ["deploy.yaml", "deploy_dev.yaml", "deploy_staging.yaml"]

# The one host reachable at /metrics from outside: UptimeRobot probes it, and
# MetricsAccessMiddleware -- not the ingress -- decides who gets an answer.
METRICS_PUBLIC_HOSTS = {"monitor.fighthealthinsurance.com"}

# The source range the deny Ingresses allow: loopback, which no client outside
# the ingress controller can ever present.
DENY_ALL_SOURCE_RANGE = "127.0.0.1/32"

# A pod IP: private, and reached without going through the ingress.
IN_CLUSTER_ADDR = "10.42.0.7"
# The ingress pod, i.e. what REMOTE_ADDR is for anything from the internet.
INGRESS_ADDR = "10.42.9.9"
UPTIMEROBOT_ADDR = UPTIMEROBOT_IPS[0]


def _ok(_request):
    return HttpResponse("metrics")


class MetricsAccessMiddlewareTest(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.middleware = MetricsAccessMiddleware(_ok)

    def test_direct_pod_scrape_is_served(self):
        request = self.factory.get("/metrics", REMOTE_ADDR=IN_CLUSTER_ADDR)
        response = self.middleware(request)
        self.assertEqual(response.status_code, 200)

    def test_loopback_scrape_is_served(self):
        request = self.factory.get("/metrics", REMOTE_ADDR="127.0.0.1")
        response = self.middleware(request)
        self.assertEqual(response.status_code, 200)

    def test_ipv6_unique_local_scrape_is_served(self):
        request = self.factory.get("/metrics", REMOTE_ADDR="fd00::1")
        response = self.middleware(request)
        self.assertEqual(response.status_code, 200)

    def test_public_peer_address_gets_404(self):
        request = self.factory.get("/metrics", REMOTE_ADDR="203.0.113.5")
        response = self.middleware(request)
        self.assertEqual(response.status_code, 404)

    def test_ingress_forwarded_request_gets_404(self):
        """The ingress pod IP is private, so X-Forwarded-For is what gives it away."""
        request = self.factory.get(
            "/metrics",
            REMOTE_ADDR=IN_CLUSTER_ADDR,
            HTTP_X_FORWARDED_FOR="203.0.113.5",
        )
        response = self.middleware(request)
        self.assertEqual(response.status_code, 404)

    def test_x_real_ip_header_gets_404(self):
        request = self.factory.get(
            "/metrics", REMOTE_ADDR=IN_CLUSTER_ADDR, HTTP_X_REAL_IP="203.0.113.5"
        )
        response = self.middleware(request)
        self.assertEqual(response.status_code, 404)

    def test_forwarded_header_gets_404(self):
        request = self.factory.get(
            "/metrics", REMOTE_ADDR=IN_CLUSTER_ADDR, HTTP_FORWARDED="for=203.0.113.5"
        )
        response = self.middleware(request)
        self.assertEqual(response.status_code, 404)

    def test_x_forwarded_host_header_gets_404(self):
        request = self.factory.get(
            "/metrics",
            REMOTE_ADDR=IN_CLUSTER_ADDR,
            HTTP_X_FORWARDED_HOST="www.fighthealthinsurance.com",
        )
        response = self.middleware(request)
        self.assertEqual(response.status_code, 404)

    def test_trailing_slash_spelling_is_also_gated(self):
        request = self.factory.get("/metrics/", REMOTE_ADDR="203.0.113.5")
        response = self.middleware(request)
        self.assertEqual(response.status_code, 404)

    def test_empty_forwarding_header_still_counts_as_forwarded(self):
        """An empty X-Forwarded-For must not read as "no proxy involved" --
        the ingress's own peer address would otherwise pass the CIDR check."""
        request = self.factory.get(
            "/metrics", REMOTE_ADDR=INGRESS_ADDR, HTTP_X_FORWARDED_FOR=""
        )
        response = self.middleware(request)
        self.assertEqual(response.status_code, 404)

    def test_unparseable_peer_address_gets_404(self):
        request = self.factory.get("/metrics", REMOTE_ADDR="")
        response = self.middleware(request)
        self.assertEqual(response.status_code, 404)

    def test_other_paths_are_untouched(self):
        request = self.factory.get(
            "/ziggy/rest/ping",
            REMOTE_ADDR=IN_CLUSTER_ADDR,
            HTTP_X_FORWARDED_FOR="203.0.113.5",
        )
        response = self.middleware(request)
        self.assertEqual(response.status_code, 200)

    def test_uptimerobot_probe_through_the_ingress_is_served(self):
        request = self.factory.get(
            "/metrics",
            REMOTE_ADDR=INGRESS_ADDR,
            HTTP_X_REAL_IP=UPTIMEROBOT_ADDR,
            HTTP_X_FORWARDED_FOR=UPTIMEROBOT_ADDR,
        )
        response = self.middleware(request)
        self.assertEqual(response.status_code, 200)

    def test_uptimerobot_probe_identified_from_forwarded_for_alone(self):
        request = self.factory.get(
            "/metrics",
            REMOTE_ADDR=INGRESS_ADDR,
            HTTP_X_FORWARDED_FOR=UPTIMEROBOT_ADDR,
        )
        response = self.middleware(request)
        self.assertEqual(response.status_code, 200)

    def test_spoofed_uptimerobot_entry_in_forwarded_for_is_ignored(self):
        """Only the last X-Forwarded-For entry is the ingress's; earlier ones
        are whatever the client sent."""
        request = self.factory.get(
            "/metrics",
            REMOTE_ADDR=INGRESS_ADDR,
            HTTP_X_FORWARDED_FOR=f"{UPTIMEROBOT_ADDR}, 203.0.113.5",
        )
        response = self.middleware(request)
        self.assertEqual(response.status_code, 404)

    def test_x_real_ip_wins_over_a_spoofed_forwarded_for(self):
        """ingress-nginx overwrites X-Real-IP, so it beats a client-chosen XFF."""
        request = self.factory.get(
            "/metrics",
            REMOTE_ADDR=INGRESS_ADDR,
            HTTP_X_REAL_IP="203.0.113.5",
            HTTP_X_FORWARDED_FOR=UPTIMEROBOT_ADDR,
        )
        response = self.middleware(request)
        self.assertEqual(response.status_code, 404)

    def test_other_external_client_through_the_ingress_gets_404(self):
        request = self.factory.get(
            "/metrics", REMOTE_ADDR=INGRESS_ADDR, HTTP_X_REAL_IP="203.0.113.5"
        )
        response = self.middleware(request)
        self.assertEqual(response.status_code, 404)

    @override_settings(METRICS_ALLOWED_FORWARDED_CIDRS=["198.51.100.0/24"])
    def test_extra_forwarded_cidrs_are_honored(self):
        middleware = MetricsAccessMiddleware(_ok)
        allowed = middleware(
            self.factory.get(
                "/metrics", REMOTE_ADDR=INGRESS_ADDR, HTTP_X_REAL_IP="198.51.100.9"
            )
        )
        # The UptimeRobot list still applies alongside the configured extras.
        uptimerobot = middleware(
            self.factory.get(
                "/metrics", REMOTE_ADDR=INGRESS_ADDR, HTTP_X_REAL_IP=UPTIMEROBOT_ADDR
            )
        )
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(uptimerobot.status_code, 200)

    @override_settings(METRICS_ALLOWED_CIDRS=["100.64.0.0/10"])
    def test_configured_cidrs_replace_the_defaults(self):
        middleware = MetricsAccessMiddleware(_ok)
        allowed = middleware(self.factory.get("/metrics", REMOTE_ADDR="100.64.1.2"))
        denied = middleware(self.factory.get("/metrics", REMOTE_ADDR=IN_CLUSTER_ADDR))
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(denied.status_code, 404)

    @override_settings(METRICS_ALLOWED_CIDRS=["not-a-cidr"])
    def test_unparseable_cidrs_fail_closed(self):
        middleware = MetricsAccessMiddleware(_ok)
        response = middleware(self.factory.get("/metrics", REMOTE_ADDR=IN_CLUSTER_ADDR))
        self.assertEqual(response.status_code, 404)


class MetricsEndpointTest(TestCase):
    """End to end through the real MIDDLEWARE stack and URLconf."""

    def test_in_cluster_scrape_gets_metrics(self):
        response = self.client.get("/metrics", REMOTE_ADDR=IN_CLUSTER_ADDR)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"# HELP", response.content)

    def test_request_through_the_ingress_gets_404(self):
        response = self.client.get(
            "/metrics",
            REMOTE_ADDR=IN_CLUSTER_ADDR,
            HTTP_X_FORWARDED_FOR="203.0.113.5",
        )
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(b"# HELP", response.content)


class PodIpHostTest(TestCase):
    """A PodMonitor scrape addresses the pod by IP, so the Host is that IP."""

    PROD_LIKE_HOSTS = ["www.fighthealthinsurance.com"]

    @override_settings(ALLOWED_HOSTS=PROD_LIKE_HOSTS + [IN_CLUSTER_ADDR])
    def test_scrape_by_pod_ip_is_served_when_pod_ip_is_allowed(self):
        response = self.client.get(
            "/metrics", REMOTE_ADDR=IN_CLUSTER_ADDR, HTTP_HOST=IN_CLUSTER_ADDR
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"# HELP", response.content)

    @override_settings(ALLOWED_HOSTS=PROD_LIKE_HOSTS)
    def test_scrape_by_pod_ip_is_rejected_without_it(self):
        """Guards settings.POD_IP: without the pod IP in ALLOWED_HOSTS every
        scrape 400s as a DisallowedHost before reaching the metrics view."""
        response = self.client.get(
            "/metrics", REMOTE_ADDR=IN_CLUSTER_ADDR, HTTP_HOST=IN_CLUSTER_ADDR
        )
        self.assertEqual(response.status_code, 400)


class UptimeRobotIPListTest(TestCase):
    """The generated probe list is what opens the monitor host; keep it sane."""

    def test_list_is_populated_and_parseable(self):
        self.assertGreater(len(UPTIMEROBOT_IPS), 20)
        for address in UPTIMEROBOT_IPS:
            ip_address(address)  # raises ValueError on a malformed entry

    def test_list_contains_no_private_addresses(self):
        """A private range here would hand every in-cluster caller the exception."""
        private = [a for a in UPTIMEROBOT_IPS if ip_address(a).is_private]
        self.assertEqual(private, [])


class PodMonitorWiringTest(TestCase):
    """The in-cluster scrape path only works if all three pieces line up."""

    def setUp(self):
        docs = [
            doc
            for doc in yaml.safe_load_all((REPO_ROOT / "k8s" / "deploy.yaml").read_text())
            if doc
        ]
        self.web = next(
            d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "web"
        )
        self.container = self.web["spec"]["template"]["spec"]["containers"][0]
        self.podmonitor = yaml.safe_load(
            (REPO_ROOT / "k8s" / "fhi-web-podmonitor.yaml").read_text()
        )

    def test_pod_ip_is_injected_from_the_downward_api(self):
        """Without it the scrape's Host is an unknown pod IP and Django 400s."""
        pod_ip = next(
            (e for e in self.container["env"] if e["name"] == "POD_IP"),
            None,
        )
        self.assertIsNotNone(pod_ip, "web container has no POD_IP env")
        self.assertEqual(pod_ip["valueFrom"]["fieldRef"]["fieldPath"], "status.podIP")

    def test_podmonitor_selects_the_web_pods(self):
        self.assertEqual(
            self.podmonitor["spec"]["selector"]["matchLabels"].items()
            - self.web["spec"]["template"]["metadata"]["labels"].items(),
            set(),
            "PodMonitor selector does not match the web pod labels",
        )

    def test_podmonitor_scrapes_a_port_the_container_exposes(self):
        endpoint = self.podmonitor["spec"]["podMetricsEndpoints"][0]
        self.assertIn(
            endpoint["port"], {p["name"] for p in self.container["ports"]}
        )
        self.assertEqual(endpoint["path"], "/metrics")

    def test_deploy_script_applies_the_podmonitor(self):
        build = (REPO_ROOT / "scripts" / "build.sh").read_text()
        self.assertIn("k8s/fhi-web-podmonitor.yaml", build)


class MetricsIngressDenyTest(TestCase):
    """Every externally served host needs a /metrics deny rule in its manifest."""

    def _ingresses(self, manifest):
        docs = yaml.safe_load_all((REPO_ROOT / "k8s" / manifest).read_text())
        return [d for d in docs if d and d.get("kind") == "Ingress"]

    def test_every_public_host_denies_metrics(self):
        for manifest in DEPLOY_MANIFESTS:
            ingresses = self._ingresses(manifest)
            self.assertTrue(ingresses, f"no Ingress found in k8s/{manifest}")
            served = set()
            denied = set()
            for ingress in ingresses:
                allowlist = (ingress["metadata"].get("annotations") or {}).get(
                    "nginx.ingress.kubernetes.io/whitelist-source-range"
                )
                for rule in ingress["spec"]["rules"]:
                    for path in rule["http"]["paths"]:
                        if path["path"] == "/metrics":
                            # A rule only denies if nothing can match the
                            # allowlist and it beats the "/" Prefix rule --
                            # "0.0.0.0/0" or a Prefix path would leave the
                            # endpoint public while still looking like a deny.
                            self.assertEqual(
                                allowlist,
                                DENY_ALL_SOURCE_RANGE,
                                f"k8s/{manifest}: /metrics rule for "
                                f"{rule['host']} has a reachable source range",
                            )
                            self.assertEqual(
                                path["pathType"],
                                "Exact",
                                f"k8s/{manifest}: /metrics rule for "
                                f"{rule['host']} must be an Exact path",
                            )
                            denied.add(rule["host"])
                        elif path["path"] == "/":
                            served.add(rule["host"])
            self.assertEqual(
                served - denied,
                served & METRICS_PUBLIC_HOSTS,
                f"k8s/{manifest} serves {sorted(served - denied)} without a "
                "/metrics deny rule",
            )
