"""fighthealthinsurance.thread_timeout.ThreadingTimeout, the stand-in for
stopit's: the same interface (a context that is falsy once its block ran out
of time, swallow_exc, cancel()), plus the cases stopit got wrong -- an
inner block eating an outer block's timeout, and a timeout that fired as the
block was ending reaching the code after it."""

import threading
import time

import pytest

from fighthealthinsurance.thread_timeout import ThreadingTimeout, TimeoutException

SHORT = 0.05
#: Long enough past SHORT for the timer to have fired.
PAST_SHORT = 0.3


def test_block_that_finishes_in_time_reads_as_executed():
    with ThreadingTimeout(5) as ctx:
        pass
    assert (bool(ctx), ctx.state) == (True, ThreadingTimeout.EXECUTED)


def test_block_that_runs_too_long_is_stopped_and_reads_as_timed_out():
    reached_end = False
    with ThreadingTimeout(SHORT) as ctx:
        time.sleep(PAST_SHORT)
        reached_end = True
    assert (bool(ctx), ctx.state, reached_end) == (
        False,
        ThreadingTimeout.TIMED_OUT,
        False,
    )


def test_busy_python_loop_is_interrupted():
    with ThreadingTimeout(SHORT) as ctx:
        while True:
            pass
    assert not ctx


def test_swallow_exc_false_raises_the_timeout():
    with pytest.raises(TimeoutException):
        with ThreadingTimeout(SHORT, swallow_exc=False):
            time.sleep(PAST_SHORT)


def test_other_exceptions_propagate_and_the_context_stays_truthy():
    with pytest.raises(ValueError):
        with ThreadingTimeout(5) as ctx:
            raise ValueError("boom")
    assert ctx


def test_timer_does_not_fire_after_the_block_has_exited():
    with ThreadingTimeout(SHORT):
        pass
    # Would raise TimeoutException here if the timer were left running.
    time.sleep(PAST_SHORT)


def test_timeout_caught_inside_the_block_still_reads_as_timed_out():
    with ThreadingTimeout(SHORT) as ctx:
        try:
            time.sleep(PAST_SHORT)
        except Exception:
            pass
    assert not ctx


def test_timeout_fired_as_the_block_ends_does_not_reach_the_code_after_it():
    # The timer firing between the block's last statement and the timeout
    # being delivered: it is withdrawn on exit, and still counts as a timeout.
    with ThreadingTimeout(60) as ctx:
        ctx._expire()
    for _ in range(10_000):  # delivery points, were it still pending
        pass
    time.sleep(0)
    assert ctx.state == ThreadingTimeout.TIMED_OUT


def test_inner_block_does_not_swallow_an_outer_blocks_timeout():
    ran_on_past_outer_limit = False
    with ThreadingTimeout(SHORT) as outer:
        with ThreadingTimeout(60) as inner:
            time.sleep(PAST_SHORT)
        ran_on_past_outer_limit = True
    assert (bool(outer), inner.state, ran_on_past_outer_limit) == (
        False,
        ThreadingTimeout.INTERRUPTED,
        False,
    )


def test_cancel_lifts_the_limit():
    with ThreadingTimeout(SHORT) as ctx:
        ctx.cancel()
        time.sleep(PAST_SHORT)
    assert (bool(ctx), ctx.state) == (True, ThreadingTimeout.CANCELED)


def test_timeout_only_interrupts_the_thread_that_entered_the_block():
    other_finished = threading.Event()

    def other_thread_work():
        time.sleep(PAST_SHORT)
        other_finished.set()

    worker = threading.Thread(target=other_thread_work)
    worker.start()
    with ThreadingTimeout(SHORT) as ctx:
        time.sleep(PAST_SHORT)
    worker.join()
    assert (bool(ctx), other_finished.is_set()) == (False, True)


def test_timer_thread_is_a_daemon():
    with ThreadingTimeout(60) as ctx:
        assert ctx._timer is not None and ctx._timer.daemon
