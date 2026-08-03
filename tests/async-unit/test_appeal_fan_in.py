"""The appeal fan-in must stream models in the order they actually finish.

Pinned regression: mappers used to be pool-submitted generator FUNCTIONS, so
their futures resolved instantly (generator construction), as_completed
ordering was meaningless, and the sub-iterators drained serially in arbitrary
order -- one hung backend at the front blocked every other model's finished
appeals until the overall budget expired and they were abandoned with it.
"""

import time
from concurrent.futures import Future, ThreadPoolExecutor
from unittest.mock import MagicMock

from fighthealthinsurance.generate_appeal import (
    GeneratedAppeal,
    _appeals_as_futures_complete,
    _generated_to_appeals_text,
)
from fighthealthinsurance.utils import UnwrapIterator


def _mapper(name, future):
    """Minimal stand-in for _generated_to_appeals_text: wait, then map."""
    for _kind, text in future.result():
        yield GeneratedAppeal(text=text, model_name=name, context_level="full")


class TestAppealsAsFuturesComplete:
    def test_yields_in_completion_order_not_submission_order(self):
        slow: Future = Future()
        fast: Future = Future()
        pairs = [
            (slow, _mapper("slow-model", slow)),
            (fast, _mapper("fast-model", fast)),
        ]

        with ThreadPoolExecutor(max_workers=1) as pool:

            def resolve():
                fast.set_result([("full", "The fast model's appeal text here.")])
                time.sleep(0.2)
                slow.set_result([("full", "The slow model's appeal text here.")])

            pool.submit(resolve)
            results = list(_appeals_as_futures_complete(pairs))

        assert [r.model_name for r in results] == ["fast-model", "slow-model"]

    def test_deadline_expiry_still_drains_pending_mappers(self):
        never: Future = Future()
        done: Future = Future()
        done.set_result([("full", "A finished appeal from the healthy model.")])

        drained = []

        def abandoned_mapper():
            # Mirrors the real mapper's behavior once the deadline has
            # passed: it notices, records, and yields nothing.
            drained.append(True)
            return
            yield  # pragma: no cover - marks this as a generator

        pairs = [
            (done, _mapper("healthy", done)),
            (never, abandoned_mapper()),
        ]
        results = list(
            _appeals_as_futures_complete(pairs, deadline=time.monotonic() - 20.0)
        )
        # The healthy model's appeal is delivered and the hung model's mapper
        # was still drained (so its attempt row gets recorded).
        assert [r.model_name for r in results] == ["healthy"]
        assert drained == [True]
        # Unblock the leaked future so nothing lingers.
        never.set_result([])


class TestMapperOutcomeRanking:
    def test_runt_then_crash_records_error_not_runt_only(self):
        """A model that yields a runt and THEN crashes mid-results must be
        filed as `error` (the crash is the actionable fact), not as
        `runt_only` (which reads as 'answered too briefly')."""

        def _exploding_results():
            yield ("full", "no.")
            raise RuntimeError("backend fell over mid-stream")

        fut: Future = Future()
        fut.set_result(_exploding_results())
        recorded = []
        recorder = MagicMock()
        recorder.record.side_effect = lambda rec: recorded.append(rec)
        template_generator = MagicMock()
        items = list(
            _generated_to_appeals_text(
                "flaky-model", fut, template_generator, "full", recorder
            )
        )
        # The runt was still delivered before the crash.
        assert [i.text for i in items] == ["no."]
        assert [r.outcome for r in recorded] == ["error"]
        assert "backend fell over" in recorded[0].error_detail


class TestUnwrapIteratorIterative:
    def test_many_empty_subiterators_do_not_recurse(self):
        # 5000 empty sub-iterators then one with a value: the old recursive
        # implementation blew the stack around ~1000.
        iterators = iter([iter([]) for _ in range(5000)] + [iter(["x"])])
        assert list(UnwrapIterator(iterators)) == ["x"]
