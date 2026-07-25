"""Tests for SpeculativeAppealsActor under the sync-actor tox env.

These run against a REAL local Ray cluster, which is the only place the actor's
own execution model gets exercised. ``prefetch_for_denial`` is an async method on
an async Ray actor, and the helper it awaits mixes Django's native async ORM with
a ``database_sync_to_async(..., thread_sensitive=False)`` bridge around the
blocking generation. None of that is covered by the mocked helper tests in
tests/sync/test_speculative_appeals.py: those drive the sync shim from a plain
thread, where asgiref routes thread-sensitive work back onto the caller. Inside a
Ray actor there is a long-lived event loop the test process does not own, so a
bridge that deadlocks or an async ORM call that cannot find a connection would
only show up here.

No ML backend is reachable from the actor process, so generation yields nothing
and the persisted-row count is 0 throughout. That is deliberate: what these
assert is that each path completes, returns an int across the Ray boundary, and
does so within a bounded time rather than hanging.
"""

import os
import time

import pytest
import ray
from django.test import TransactionTestCase

from fighthealthinsurance.models import Denial, ProposedAppeal
from fighthealthinsurance.speculative_appeals_actor import SpeculativeAppealsActor

# The actor bootstraps Django inside a fresh Ray worker (settings, urlconf, app
# registry) before it can serve anything, and a cold start in CI can take 20+s.
ACTOR_BOOT_TIMEOUT = 120
# Generous, but finite on purpose: a bridge that deadlocks inside the actor's
# event loop fails as a timeout here instead of hanging the suite.
PREFETCH_TIMEOUT = 120


@pytest.mark.django_db
class TestSpeculativeAppealsActorRay(TransactionTestCase):
    fixtures = ["fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        if not ray.is_initialized():
            environ = dict(os.environ)
            # TestActor (not Test) so the Ray worker and this process share a
            # file-based SQLite DB. Under "Test" the actor gets its own
            # in-memory DB, cannot see the denial created here, and every
            # assertion below would pass vacuously.
            environ["DJANGO_CONFIGURATION"] = "TestActor"
            ray.init(
                namespace="fhi-test",
                ignore_reinit_error=True,
                runtime_env={"env_vars": environ},
                num_cpus=1,
            )
        self.actor = SpeculativeAppealsActor.remote()
        # Boot once here rather than per-test so a slow cold start is not
        # mistaken for a slow prefetch.
        assert (
            ray.get(self.actor.hello.remote(), timeout=ACTOR_BOOT_TIMEOUT) == "Hi"
        ), "actor failed to bootstrap Django"

    def tearDown(self):
        if ray.is_initialized():
            ray.shutdown()

    def _denial(self, **overrides) -> Denial:
        defaults = dict(
            hashed_email="hash:speculative-actor",
            denial_text="I was denied an MRI of the lumbar spine for chronic pain.",
        )
        defaults.update(overrides)
        return Denial.objects.create(**defaults)

    def test_prefetch_returns_zero_for_a_denial_with_no_text(self):
        """The earliest exit, and the one that proves the plumbing.

        Reaching the "no text" branch at all means the actor awaited the helper,
        the native async ORM ran a select inside the actor's event loop against
        the shared DB, and the int came back over the Ray boundary.
        """
        denial = self._denial(denial_text="")

        result = ray.get(
            self.actor.prefetch_for_denial.remote(denial.denial_id),
            timeout=PREFETCH_TIMEOUT,
        )

        assert result == 0

    def test_prefetch_returns_zero_for_a_missing_denial(self):
        """A denial_id that no longer exists must be reported, not raised --
        the dispatch is fire-and-forget, so an exception here would surface as
        an unretrievable Ray task error rather than anything actionable."""
        result = ray.get(
            self.actor.prefetch_for_denial.remote(999_999_999),
            timeout=PREFETCH_TIMEOUT,
        )

        assert result == 0

    def test_prefetch_is_idempotent_against_an_existing_reserve(self):
        """The aexists() idempotency guard must work in the actor process."""
        denial = self._denial()
        ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text="A reserve appeal that already exists for this denial.",
            speculative=True,
            context_level="speculative",
        )

        result = ray.get(
            self.actor.prefetch_for_denial.remote(denial.denial_id),
            timeout=PREFETCH_TIMEOUT,
        )

        assert result == 0
        # Untouched: the guard returned before generating, so no duplicate.
        assert ProposedAppeal.objects.filter(for_denial=denial).count() == 1

    def test_forced_prefetch_runs_the_bridged_generation_without_deadlocking(self):
        """force=True walks past the idempotency guard into the real generation.

        This is the path that matters for the async rewrite: inside the actor's
        event loop the helper awaits database_sync_to_async(_generate_drafts,
        thread_sensitive=False), and around it awaits the native async ORM. No ML
        backend is reachable, so the reserve stays empty and the count is 0 --
        the assertion carrying weight is that it RETURNS. A bridge that
        deadlocked against the actor's loop, or an async ORM call that could not
        get a connection in the worker, would time out here instead.
        """
        denial = self._denial()
        ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text="An already-served reserve appeal, kept for analytics.",
            speculative=False,
            context_level="speculative",
        )

        started = time.monotonic()
        result = ray.get(
            self.actor.prefetch_for_denial.remote(denial.denial_id, force=True),
            timeout=PREFETCH_TIMEOUT,
        )
        elapsed = time.monotonic() - started

        assert isinstance(result, int)
        assert elapsed < PREFETCH_TIMEOUT
        # The promoted row is analytics history and must survive a re-run.
        assert ProposedAppeal.objects.filter(
            for_denial=denial, speculative=False
        ).exists()

    def test_actor_survives_a_prefetch_and_serves_the_next_one(self):
        """The helper's never-raises contract has to hold at the actor boundary
        too: a detached actor that dies on one bad denial stops precomputing for
        every later one, silently."""
        missing = ray.get(
            self.actor.prefetch_for_denial.remote(999_999_998),
            timeout=PREFETCH_TIMEOUT,
        )
        assert missing == 0

        denial = self._denial(denial_text="")
        assert (
            ray.get(
                self.actor.prefetch_for_denial.remote(denial.denial_id),
                timeout=PREFETCH_TIMEOUT,
            )
            == 0
        )
        assert ray.get(self.actor.hello.remote(), timeout=ACTOR_BOOT_TIMEOUT) == "Hi"
