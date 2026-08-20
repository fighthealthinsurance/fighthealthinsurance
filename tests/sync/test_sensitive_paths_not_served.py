"""Sensitive filenames must never resolve to a real response.

Scanners probe every public site for config and credential files -- .env,
key.pem, settings.json, .git/config -- and several such probes reached Sentry
in a single week. Every one 404'd, which is the *good* outcome: an actual
exposure would return 200 and raise nothing at all, so error monitoring is
silent on precisely the case that matters.

This test inverts that. It stays quiet while these paths are unroutable and
fails the moment one starts serving content, which is how such exposures
normally happen: a new route, a catch-all handler, or a misconfigured storage
backend that quietly starts answering for paths nobody audited.

Requires a 4xx on the *final* response, following redirects. Following them
is the point: a bare "3xx is fine" allow-list passes a catch-all that
redirects to a signed storage URL serving the file, and passes vacuously if
any site-wide redirect (SECURE_SSL_REDIRECT, PREPEND_WWW, a populated
DOMAIN_REDIRECTS) is ever switched on. A 2xx means we served it; a 5xx means
the request reached something that broke rather than something that refused,
which can hide a routing bug behind an apparent "not served".

Scope: this covers what Django routes. In production nginx answers /static
and /media off local disk before uvicorn is reached (conf/nginx.default), so
no Django test can observe that surface -- guard it in the nginx config and
in collectstatic's ignore list, not here.
"""

from django.test import TestCase

# Paths taken from probes actually observed against production, plus the
# usual suspects from scanner wordlists. Add to this list, never trim it.
SENSITIVE_PATHS = [
    # observed in production Sentry, Aug 2026
    ".bashrc",
    "api/.env",
    "key.pem",
    "settings.json",
    # standard scanner fare
    ".env",
    ".env.local",
    ".env.production",
    ".git/config",
    ".git/HEAD",
    "config.json",
    "credentials.json",
    "secrets.json",
    ".aws/credentials",
    "id_rsa",
    "private.pem",
    "backup.sql",
    "dump.sql",
    "docker-compose.yml",
    "Dockerfile",
    ".npmrc",
    ".dockercfg",
    "wp-config.php",
]


class SensitivePathsNotServedTest(TestCase):
    """None of these may return a successful response.

    TestCase, not SimpleTestCase: rendering a 404 runs the full middleware
    stack and 404.html -> base.html, whose site_banner_context queries
    SiteBanner. Under SimpleTestCase that raises DatabaseOperationForbidden
    on every request, and the suite only stays green because that context
    processor happens to swallow it.
    """

    def setUp(self):
        # Return the 500 instead of re-raising it. With the default, a
        # catch-all that *raises* on one path aborts the whole loop, leaving
        # every later path unprobed -- and the documented "a 5xx fails" rule
        # unreachable.
        self.client.raise_request_exception = False

    def assert_paths_refuse(self, paths):
        for path in paths:
            with self.subTest(path=path):
                response = self.client.get(f"/{path}", follow=True)
                self.assertTrue(
                    400 <= response.status_code < 500,
                    f"/{path} answered {response.status_code} (expected a 4xx "
                    f"refusal) and may be exposing configuration or "
                    f"credentials. If a route legitimately needs this name, "
                    f"rename the route -- do not remove the path from "
                    f"SENSITIVE_PATHS.",
                )

    def test_sensitive_paths_are_not_served(self):
        self.assert_paths_refuse(SENSITIVE_PATHS)
