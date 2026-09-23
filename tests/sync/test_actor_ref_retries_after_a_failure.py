"""A creation that fails must not be remembered.

``BaseActorRef.get`` used to be a ``cached_property`` that stored whatever
``.remote()`` returned. In client mode that call returns before the server has
confirmed the actor exists, so a creation that then failed left the process
holding a handle to an actor that was never made, and every later call raised
``'InProgressSentinel' object has no attribute 'id'`` instead of trying again.

In production that meant one bad websocket disconnect disabled the denied-items
analysis for the whole life of that pod.
"""

from django.test import SimpleTestCase

from fighthealthinsurance.base_actor_ref import BaseActorRef


class _Options:
    def __init__(self, owner):
        self._owner = owner

    def remote(self):
        self._owner.creations += 1
        if self._owner.fail_creations:
            raise RuntimeError("cannot pickle '_thread.RLock' object")
        return _Handle(self._owner)


class _Handle:
    def __init__(self, owner):
        self._owner = owner

    @property
    def run(self):
        return self

    def remote(self):
        self._owner.runs += 1
        if self._owner.fail_runs:
            raise AttributeError("'InProgressSentinel' object has no attribute 'id'")
        return "task-handle"


class _FakeActorClass:
    """Stands in for a @ray.remote class without needing a cluster."""

    def __init__(self):
        self.creations = 0
        self.runs = 0
        self.fail_creations = False
        self.fail_runs = False

    def options(self, **kwargs):
        return _Options(self)


class ARefThatFailedRetriesTest(SimpleTestCase):
    def _ref(self, *, has_run_method=False):
        fake = _FakeActorClass()

        class Ref(BaseActorRef):
            actor_class = fake  # type: ignore[assignment]
            actor_name = "test_actor"

        Ref.has_run_method = has_run_method
        ref = Ref()
        ref._actor_instance = None
        return ref, fake

    def test_a_failed_creation_is_not_remembered(self):
        """Holds on the old code too, and is here so it keeps holding.

        The old version assigned the result of ``.remote()``, so a call that
        raised never reached the assignment. A refactor that reserves the
        slot before calling would break that quietly, which is what this
        pins. The test below is the one that fails on the old code.
        """
        ref, fake = self._ref()
        fake.fail_creations = True

        with self.assertRaises(RuntimeError):
            ref.get

        self.assertIsNone(
            ref._actor_instance, "a handle to an actor that was never made was kept"
        )

        # The next caller gets a working actor rather than the dead handle.
        fake.fail_creations = False
        handle = ref.get

        self.assertIsInstance(handle, _Handle)
        self.assertEqual(fake.creations, 2, "the second call did not retry")

    def test_a_successful_creation_is_reused(self):
        ref, fake = self._ref()

        first = ref.get
        second = ref.get

        self.assertIs(first, second)
        self.assertEqual(fake.creations, 1, "the actor was created twice")

    def test_the_relaunch_path_clears_both_places(self):
        """``get`` is a cached_property, so there are two things to clear.

        ``relaunch_actors`` clears ``_actor_instance`` and deletes the
        cached entry after killing an actor. Both are needed: the handle
        lives in one and the value ``get`` last returned lives in the other.
        This pins that, because clearing only one hands back the dead
        handle, which is the bug the kill path exists to avoid.
        """
        ref, fake = self._ref()

        first = ref.get
        self.assertEqual(fake.creations, 1)

        ref._actor_instance = None
        self.assertIs(ref.get, first, "the cached entry still answers")

        # Driven through the production code, not by clearing it here: a
        # test that does the clearing itself stays green if relaunch_actors
        # stops doing it, which is the regression worth catching.
        from unittest.mock import patch

        from fighthealthinsurance import actor_health_status

        ref.has_run_method = True  # relaunch_actors unpacks (actor, task)

        with patch(
            "fighthealthinsurance.email_polling_actor_ref.email_polling_actor_ref",
            ref,
        ), patch.object(
            actor_health_status.ray, "get_actor", side_effect=ValueError("gone")
        ):
            actor_health_status.relaunch_actors(force=True)

        second, _task = ref.get

        self.assertIsNot(second, first)
        self.assertEqual(fake.creations, 2)

    def test_a_failed_run_clears_the_handle_too(self):
        """The handle is no good to the next caller either.

        This is the shape the production failure actually took: creation
        appeared to succeed and the failure surfaced on the call after it.
        """
        ref, fake = self._ref(has_run_method=True)
        fake.fail_runs = True

        with self.assertRaises(AttributeError):
            ref.get

        self.assertIsNone(ref._actor_instance)

        fake.fail_runs = False
        actor, task = ref.get

        self.assertEqual(task, "task-handle")
        self.assertEqual(fake.creations, 2, "it reused the handle whose run failed")


class TheFirstUseIsOutsideGetTest(SimpleTestCase):
    """``get`` cannot see the call that actually proves the actor exists.

    Every ref without a run method hands the handle back and the caller makes
    the first call on it. In client mode that is where a creation that only
    half succeeded surfaces, and the handle is cached by then, so without a
    way to forget it the whole process keeps calling the dead one. That is
    the shape the production failure took on the websocket disconnect path.
    """

    def _ref(self):
        fake = _FakeActorClass()

        class Ref(BaseActorRef):
            actor_class = fake  # type: ignore[assignment]
            actor_name = "test_actor"

        ref = Ref()
        ref._actor_instance = None
        return ref, fake

    def test_invalidate_clears_both_places(self):
        ref, fake = self._ref()

        first = ref.get
        self.assertEqual(fake.creations, 1)

        ref.invalidate()
        second = ref.get

        self.assertIsNot(second, first, "the dead handle came back")
        self.assertEqual(fake.creations, 2)

    def test_the_disconnect_path_forgets_a_handle_whose_first_call_failed(self):
        """Driven through the function the disconnect handler calls."""
        from unittest.mock import patch

        from fighthealthinsurance import websockets

        ref, fake = self._ref()
        handle = ref.get

        def explode(**kwargs):
            raise AttributeError(
                "'InProgressSentinel' object has no attribute 'id'"
            )

        handle.run_analysis = type("M", (), {"remote": staticmethod(explode)})()

        with patch(
            "fighthealthinsurance.denied_items_analysis_actor_ref."
            "denied_items_analysis_actor_ref",
            ref,
        ), patch(
            "fighthealthinsurance.base_actor_ref.ray_cluster_available",
            return_value=True,
        ):
            from asgiref.sync import async_to_sync

            with self.assertRaises(AttributeError):
                async_to_sync(websockets.enqueue_denied_items_analysis)(
                    chat_id="chat-1"
                )

        self.assertIsNone(
            ref._actor_instance, "the disconnect path kept the dead handle"
        )
        self.assertNotIn("get", ref.__dict__, "the cached value is still there")


class RaysOwnCacheIsClearedTest(SimpleTestCase):
    """The poisoned state is Ray's, not ours.

    ``ClientActorClass._ensure_ref`` sets ``_ref`` to an ``InProgressSentinel``
    before pickling the class, so a pickle that raises leaves that sentinel
    behind, and its retry guard is ``if self._ref is None``. Nothing tries
    again: every later call reads ``_ref.id`` and raises. That stub is cached
    by Ray and reached through an attribute Ray sets on the decorated class,
    so clearing our own handle does not touch it, which is why the fix needs
    to reach in.
    """

    def _stub_with(self, ref):
        import threading

        class Stub:
            def __init__(self):
                self._ref = ref
                self._lock = threading.Lock()

        return Stub()

    def _run_against(self, stub, key="k"):
        from unittest.mock import patch

        from fighthealthinsurance import base_actor_ref

        class Klass:
            pass

        setattr(Klass, "__ray_client_mode_key__", key)

        client_ray = type(
            "ClientRay",
            (),
            {
                "_converted_key_exists": staticmethod(lambda k: k == key),
                "_get_converted": staticmethod(lambda k: stub),
            },
        )()

        with patch.dict(
            "sys.modules",
            {"ray.util.client": type("M", (), {"ray": client_ray})()},
        ):
            base_actor_ref.clear_poisoned_client_class(Klass)

    def test_a_half_finished_export_is_cleared(self):
        from ray.util.client.common import InProgressSentinel

        stub = self._stub_with(InProgressSentinel())

        self._run_against(stub)

        self.assertIsNone(stub._ref, "the sentinel was left in place")

    def test_a_real_reference_is_left_alone(self):
        """A working export must not be thrown away."""
        real = object()
        stub = self._stub_with(real)

        self._run_against(stub)

        self.assertIs(stub._ref, real)

    def test_it_does_nothing_when_ray_looks_different(self):
        """Ray's private surface, so a shape change must be harmless."""
        from fighthealthinsurance import base_actor_ref

        class Klass:
            pass

        # No client-mode key at all: nothing to clear, and no exception.
        base_actor_ref.clear_poisoned_client_class(Klass)


class _LiveHandle:
    """A handle to an actor that already exists on the cluster."""

    def __init__(self, owner, healthy):
        self._owner = owner
        self._healthy = healthy

    @property
    def health_check(self):
        return self

    @property
    def run(self):
        return self

    def remote(self):
        # health_check.remote() and run.remote() share this; the fake ray.get
        # below turns the former into the health answer, and the owner counts
        # the latter as a run only when it comes through the run path.
        return self


class AttachingToARunningActorTest(SimpleTestCase):
    """A fresh process attaching to a live loop actor must not start a
    second loop.

    ``get`` used to call ``run.remote()`` whenever it was first evaluated in
    a process, whether it had created the actor or merely attached to one
    ``get_if_exists`` found. The loop actors are async actors, so every
    reconcile run and every re-run of the launch job added one more
    concurrent loop inside the same actor.
    """

    def _ref(self):
        fake = _FakeActorClass()

        class Ref(BaseActorRef):
            actor_class = fake  # type: ignore[assignment]
            actor_name = "test_actor"
            has_run_method = True

        ref = Ref()
        ref._actor_instance = None
        return ref, fake

    def _attach(self, ref, existing, health):
        """Drive ``get`` with a cluster attached and ``existing`` on it."""
        from unittest.mock import patch

        from fighthealthinsurance import base_actor_ref

        runs = []

        class Handle(_LiveHandle):
            @property
            def run(self):
                class Run:
                    @staticmethod
                    def remote():
                        runs.append(1)
                        return "task-handle"

                return Run()

        handle = Handle(None, health) if existing else None

        def get_actor(name, namespace):
            if handle is None:
                raise ValueError("no such actor")
            return handle

        def ray_get(ref_, timeout=None):
            if isinstance(health, Exception):
                raise health
            return health

        with patch.object(
            base_actor_ref, "ray_cluster_available", return_value=True
        ), patch.object(
            base_actor_ref.ray, "get_actor", side_effect=get_actor
        ), patch.object(base_actor_ref.ray, "get", side_effect=ray_get):
            result = ref.get
        return result, runs, handle

    def test_a_live_loop_is_attached_without_a_second_run(self):
        ref, fake = self._ref()

        (actor, task), runs, handle = self._attach(ref, existing=True, health=True)

        self.assertIs(actor, handle)
        self.assertIsNone(task, "a run task was started on a live loop")
        self.assertEqual(runs, [], "run.remote() was called on a live loop")
        self.assertEqual(fake.creations, 0, "an existing actor was recreated")

    def test_an_existing_actor_without_a_loop_gets_one(self):
        ref, fake = self._ref()

        (actor, task), runs, handle = self._attach(ref, existing=True, health=False)

        self.assertIs(actor, handle)
        self.assertEqual(task, "task-handle")
        self.assertEqual(runs, [1])
        self.assertEqual(fake.creations, 0)

    def test_an_absent_actor_is_created_and_run(self):
        ref, fake = self._ref()

        (actor, task), runs, _handle = self._attach(ref, existing=False, health=True)

        self.assertIsInstance(actor, _Handle)
        self.assertEqual(task, "task-handle")
        self.assertEqual(fake.creations, 1)
        self.assertEqual(fake.runs, 1)

    def test_an_actor_that_does_not_answer_is_left_to_ray(self):
        """A dead or hung actor answers nothing; the creation path with
        ``get_if_exists`` decides what happens, as before."""
        ref, fake = self._ref()

        (actor, task), runs, _handle = self._attach(
            ref, existing=True, health=RuntimeError("actor died")
        )

        self.assertIsInstance(actor, _Handle)
        self.assertEqual(fake.creations, 1)
        self.assertEqual(fake.runs, 1)
        self.assertEqual(runs, [], "run was sent to the unresponsive handle")

    def test_without_a_cluster_nothing_is_looked_up(self):
        """Tests and dev servers have no cluster; ``ray.get_actor`` would
        start one. The pre-check must not run there."""
        from unittest.mock import patch

        from fighthealthinsurance import base_actor_ref

        ref, fake = self._ref()
        with patch.object(
            base_actor_ref, "ray_cluster_available", return_value=False
        ), patch.object(
            base_actor_ref.ray, "get_actor", side_effect=AssertionError("looked up")
        ):
            actor, task = ref.get

        self.assertEqual(fake.creations, 1)
        self.assertEqual(task, "task-handle")
