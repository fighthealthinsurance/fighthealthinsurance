"""Sensitive filenames must never resolve to a real response.

Scanners probe every public site for config and credential files -- .env,
key.pem, settings.json, .git/config -- and four such probes reached Sentry in
a single week. Every one 404'd, which is the *good* outcome: an actual
exposure would return 200 and raise nothing at all, so error monitoring is
silent on precisely the case that matters.

This test inverts that. It stays quiet while these paths are unroutable and
fails the moment one starts serving content, which is how such exposures
normally happen: a new static-file route, a catch-all handler, or a
misconfigured storage backend that quietly starts answering for paths nobody
audited.

Deliberately asserts "not successful" rather than "== 404" -- a 403 or a
redirect to a login page is also fine. Serving the bytes is not.
"""

from django.test import Client, SimpleTestCase

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


class SensitivePathsNotServedTest(SimpleTestCase):
    """None of these may return a successful response.

    SimpleTestCase: this exercises URL routing only, so it needs no database
    and stays fast enough to never be the reason someone skips the suite.
    """

    def test_sensitive_paths_are_not_served(self):
        client = Client()
        served = []
        for path in SENSITIVE_PATHS:
            response = client.get(f"/{path}")
            # 2xx means we handed the caller content. Anything else -- 404,
            # 403, a redirect to login -- means we did not.
            if 200 <= response.status_code < 300:
                served.append((path, response.status_code))
        self.assertEqual(
            served,
            [],
            "These paths returned a successful response and may be exposing "
            f"configuration or credentials: {served}. If a route legitimately "
            "needs one of these names, rename the route -- do not remove the "
            "path from this list.",
        )

    def test_sensitive_paths_are_not_served_with_query_or_case_variants(self):
        """Same guarantee for the trivial evasions scanners actually try."""
        client = Client()
        served = []
        for path in (".ENV", ".Env", ".env?x=1", "static/.env", "media/.env"):
            response = client.get(f"/{path}")
            if 200 <= response.status_code < 300:
                served.append((path, response.status_code))
        self.assertEqual(served, [], f"Variant paths served content: {served}")
