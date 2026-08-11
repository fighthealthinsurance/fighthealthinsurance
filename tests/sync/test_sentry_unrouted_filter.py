"""The Sentry filter must drop scanner probes without swallowing real errors.

Background: bots probe every public site for exposed config (/.env, /key.pem),
Django refuses them with Resolver404, and each novel path fingerprints as a
NEW Sentry issue -- so "new issue" alerts fire for something nobody can fix.
asgi.before_send_filter drops those.

The risk in doing so is over-matching. Sentry reports chained exceptions as
several entries in ``exception.values``, so an event can legitimately contain
a Resolver404 *alongside* the real failure. Suppressing on "any entry is a
Resolver404" would discard the real error too; these tests pin the "every
entry" behaviour.
"""

from django.test import SimpleTestCase

from fighthealthinsurance.sentry_filters import is_only_unrouted


def _exc(*types):
    return [{"type": t, "value": "..."} for t in types]


class IsOnlyUnroutedTest(SimpleTestCase):
    def test_lone_scanner_probe_is_suppressed(self):
        self.assertTrue(is_only_unrouted(_exc("Resolver404")))

    def test_several_unrouted_entries_are_suppressed(self):
        self.assertTrue(is_only_unrouted(_exc("Resolver404", "Resolver404")))

    def test_mixed_chain_is_kept(self):
        """The regression this file exists for.

        A real error that passed through URL resolution carries both types.
        Dropping it would hide the failure that actually matters.
        """
        self.assertFalse(is_only_unrouted(_exc("Resolver404", "OperationalError")))
        self.assertFalse(is_only_unrouted(_exc("OperationalError", "Resolver404")))

    def test_ordinary_errors_are_kept(self):
        self.assertFalse(is_only_unrouted(_exc("OperationalError")))
        self.assertFalse(is_only_unrouted(_exc("Http404")))

    def test_deliberate_http404_is_kept(self):
        """Http404 raised inside a view (missing appeal, expired link) is a
        different class from "no route matched" and can indicate a real bug."""
        self.assertFalse(is_only_unrouted(_exc("Http404", "Resolver404")))

    def test_empty_and_missing_are_kept(self):
        """all([]) is True, so emptiness must be checked first -- otherwise
        message-only events (no exception at all) would be suppressed."""
        self.assertFalse(is_only_unrouted([]))
        self.assertFalse(is_only_unrouted(None))

    def test_entries_without_a_type_are_kept(self):
        self.assertFalse(is_only_unrouted([{"value": "no type key"}]))
