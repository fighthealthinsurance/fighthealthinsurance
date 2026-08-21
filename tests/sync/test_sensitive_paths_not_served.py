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
matters because a bare "3xx is fine" allow-list would pass vacuously if any
site-wide redirect (SECURE_SSL_REDIRECT, PREPEND_WWW, a populated
DOMAIN_REDIRECTS) were ever switched on. A 2xx means we served it; a 5xx means
the request reached something that broke rather than something that refused,
which can hide a routing bug behind an apparent "not served".

Following redirects does NOT cover an off-site one, so those are failed
outright -- see assert_no_offsite_redirect.

Scope: this covers what Django routes. In production nginx answers /static
and /media off local disk before uvicorn is reached (conf/nginx.default), so
no Django test can observe that surface -- guard it in the nginx config and
in collectstatic's ignore list, not here.
"""

from urllib.parse import urlsplit

from django.test import TestCase

# Hosts the Django test client can actually answer for. "" is a relative
# Location header (same app); "testserver" is the default test client host.
INTERNAL_HOSTS = {"", "testserver"}

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

    def assert_no_offsite_redirect(self, path, response):
        """Fail if any hop pointed off-site, rather than trusting follow=True.

        follow=True does not fetch an external URL. Django's test client
        re-dispatches only the redirect's *path* in-process against this same
        app (see Client._follow_redirect, which sets SERVER_NAME/HTTP_HOST
        from the target and then calls self.get(path)). Since ALLOWED_HOSTS is
        ["*"] under test, that synthetic request is accepted and typically
        returns this app's own 404 -- so a catch-all redirecting /.env to a
        signed storage URL that really does serve the file would pass the
        status assertion below. That is the exact exposure this file exists to
        catch, so an off-site hop is failed outright: we cannot vouch for a
        destination we never fetched.
        """
        for target, status in getattr(response, "redirect_chain", []):
            host = urlsplit(target).netloc.split("@")[-1]
            if host and host not in INTERNAL_HOSTS:
                self.fail(
                    f"/{path} redirected ({status}) off-site to {target!r}. The "
                    f"test client never fetched that URL, so this test cannot "
                    f"show the file is unserved -- verify the destination by "
                    f"hand, then narrow the redirect."
                )

    def assert_paths_refuse(self, paths):
        for path in paths:
            with self.subTest(path=path):
                response = self.client.get(f"/{path}", follow=True)
                self.assert_no_offsite_redirect(path, response)
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


class OffsiteRedirectGuardTest(TestCase):
    """The off-site guard is the only part of this file that a real exposure
    would exercise, and no fixture in the suite produces such a redirect -- so
    prove it fires rather than shipping an assertion nobody has run."""

    class _Response:
        def __init__(self, redirect_chain):
            self.redirect_chain = redirect_chain
            self.status_code = 404

    def setUp(self):
        self.case = SensitivePathsNotServedTest()

    def test_offsite_redirect_fails(self):
        response = self._Response([("https://storage.example.com/signed/.env", 302)])
        with self.assertRaises(AssertionError) as caught:
            self.case.assert_no_offsite_redirect(".env", response)
        self.assertIn("off-site", str(caught.exception))

    def test_offsite_redirect_with_credentials_in_netloc_fails(self):
        """urlsplit keeps userinfo in netloc; the host must be read past it."""
        response = self._Response([("https://user:pw@evil.example.com/.env", 302)])
        with self.assertRaises(AssertionError):
            self.case.assert_no_offsite_redirect(".env", response)

    def test_same_app_redirects_pass(self):
        response = self._Response(
            [("/login/", 302), ("http://testserver/login/", 302)]
        )
        self.case.assert_no_offsite_redirect(".env", response)

    def test_no_redirect_chain_passes(self):
        self.case.assert_no_offsite_redirect(".env", self._Response([]))
