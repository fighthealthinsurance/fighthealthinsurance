"""Every actor class has to pickle from a process that ships its logs.

Ray sends an actor class to the cluster by value, so cloudpickle walks the
class and everything its methods close over. A module-level ``logger`` is a
live object: in the web process it carries whatever sinks are attached to it,
and every stdlib ``logging.Handler`` owns a ``threading.RLock``, which does
not pickle.

That is how the denied-items analysis actor stopped being created in
production on v0.23.13a-dev. The disconnect handler logged "cannot pickle
'_thread.RLock' object" on every chat close, the exception was caught so
nobody saw anything, and the post-chat analysis simply never ran. It passed
CI and it passed locally, because the handler is attached in the ASGI process
and only when Azure Log Analytics is configured.

These tests run with a handler attached, which is what the web process looks
like, and pickle what Ray would pickle.
"""

import importlib
import logging
import pkgutil

from django.test import SimpleTestCase
from loguru import logger
from ray import cloudpickle

import fighthealthinsurance


def _actor_ref_classes():
    """Every actor class an ``*ActorRef`` in the package points at."""
    from fighthealthinsurance.base_actor_ref import BaseActorRef

    found = {}
    for module in pkgutil.iter_modules(fighthealthinsurance.__path__):
        if not module.name.endswith("_actor_ref"):
            continue
        imported = importlib.import_module(f"fighthealthinsurance.{module.name}")
        for name in dir(imported):
            value = getattr(imported, name)
            if (
                isinstance(value, type)
                and issubclass(value, BaseActorRef)
                and value is not BaseActorRef
                and getattr(value, "actor_class", None) is not None
            ):
                found[f"{module.name}.{name}"] = value.actor_class
    return found


class EveryActorClassPicklesWithLogSinksAttachedTest(SimpleTestCase):
    def setUp(self):
        # What the web process looks like once Log Analytics is configured.
        # A bare StreamHandler is enough: the lock is on Handler itself, so
        # this covers any sink anybody adds later, not just Azure's.
        self._sink_id = logger.add(logging.StreamHandler(), level="INFO")
        self.addCleanup(logger.remove, self._sink_id)

    def test_the_refs_were_actually_found(self):
        """A sweep that silently stops finding actors would pass forever."""
        found = _actor_ref_classes()

        self.assertGreaterEqual(len(found), 8, f"only found {sorted(found)}")
        self.assertIn(
            "denied_items_analysis_actor_ref.DeniedItemsAnalysisActorRef",
            found,
            "the actor this test was written for is not in the sweep",
        )

    def test_every_actor_class_pickles(self):
        for where, actor in sorted(_actor_ref_classes().items()):
            with self.subTest(actor=where):
                # Ray pickles the wrapped class, not the handle it hands back.
                target = getattr(actor, "__ray_metadata__", None)
                target = target.modified_class if target else actor
                try:
                    cloudpickle.dumps(target)
                except Exception as e:
                    self.fail(
                        f"{where} cannot be sent to the cluster from a process "
                        f"with a log sink attached: {type(e).__name__}: {e}"
                    )


class TheLogHandlerItselfPicklesTest(SimpleTestCase):
    """Second line, so a future actor that captures the logger still works."""

    def test_it_survives_a_round_trip_and_comes_back_usable(self):
        from fighthealthinsurance.log_analytics import LogAnalyticsHandler

        handler = LogAnalyticsHandler(level=logging.INFO)

        revived = cloudpickle.loads(cloudpickle.dumps(handler))

        self.assertIsInstance(revived, LogAnalyticsHandler)
        self.assertEqual(revived.level, logging.INFO)
        # A handler with no lock raises the moment anything logs through it.
        self.assertIsNotNone(revived.lock)

        # And it has to survive being logged through, not merely exist.
        # handle() is what a sink actually calls, and it takes the lock,
        # formats the record and emits; a half-restored handler dies there
        # rather than here, leaving an operator with no logs and no clue.
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="after the round trip",
            args=(),
            exc_info=None,
        )
        revived.handle(record)
        self.assertEqual(
            revived.format(record),
            "after the round trip",
            "the revived handler cannot format a record",
        )

    def test_a_logger_carrying_it_pickles(self):
        """The shape that actually broke: the sink reachable from the logger."""
        from fighthealthinsurance.log_analytics import LogAnalyticsHandler

        sink_id = logger.add(LogAnalyticsHandler(level=logging.INFO), level="INFO")
        try:

            def uses_the_module_logger():
                logger.info("hello")

            cloudpickle.dumps(uses_the_module_logger)
        finally:
            logger.remove(sink_id)
