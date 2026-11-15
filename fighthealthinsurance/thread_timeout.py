"""A time limit on a block of synchronous code, in place of ``stopit``.

``stopit`` (last released 2018) imports ``pkg_resources``, which newer
Setuptools no longer ships. This is its ``ThreadingTimeout`` with the same
interface -- ``seconds``, ``swallow_exc``, ``state``, ``cancel()`` and a
context that is falsy once the block ran out of time -- and none of its other
parts, which nothing here used.

As with ``stopit``, the limit is enforced by raising :class:`TimeoutException`
in the thread that entered the block, from a timer thread. Python delivers
such an exception only between bytecodes, so code blocked inside one system
call (a socket read, a hung mount) is not cut off until that call returns:
give those calls their own timeouts. Never use it on a thread running an
event loop, where the exception lands in whichever coroutine is running at
that moment; use ``asyncio.timeout`` there.

Where it differs from ``stopit``:

* A timeout that fired as the block was finishing, but had not reached it
  yet, is withdrawn on exit rather than raised later in whatever code runs
  next. The context still reads as timed out. (A timeout reaching the thread
  in the instant between the block's last statement and the start of
  ``__exit__`` still escapes, as with ``stopit``; Python offers no hook to
  close that window.)
* Only the context's own timeout is swallowed. Another context's (an outer
  block's) passes through, so an inner block can't eat an outer limit and
  let the outer block run on past it.
"""

import ctypes
import threading
from types import TracebackType
from typing import Optional, Type

from loguru import logger

__all__ = ["ThreadingTimeout", "TimeoutException"]


class TimeoutException(Exception):
    """Raised in the block when it runs past its time limit."""


def _set_async_exc(thread_id: int, exc: Optional[Type[BaseException]]) -> int:
    """Schedule exc in the thread, or with None withdraw one scheduled there.

    Returns how many threads it changed."""
    return int(
        ctypes.pythonapi.PyThreadState_SetAsyncExc(
            ctypes.c_ulong(thread_id),
            ctypes.py_object(exc) if exc is not None else None,
        )
    )


class ThreadingTimeout:
    """Limit a ``with`` block to ``seconds``.

    With ``swallow_exc=True`` (the default) a timed-out block exits quietly
    and the context, checked afterwards, is falsy. With ``swallow_exc=False``
    :class:`TimeoutException` propagates out of the block instead.
    """

    EXECUTED, EXECUTING, TIMED_OUT, INTERRUPTED, CANCELED = range(5)

    def __init__(self, seconds: float, swallow_exc: bool = True) -> None:
        self.seconds = seconds
        self.swallow_exc = swallow_exc
        self.state = ThreadingTimeout.EXECUTED
        self._target_tid: Optional[int] = None
        self._timer: Optional[threading.Timer] = None
        # Held by the timer while it raises and by the block's exit and
        # cancel(), so a timeout is never raised once the block is done.
        self._lock = threading.Lock()

    def __bool__(self) -> bool:
        """False once the block ran out of time (or was interrupted)."""
        return self.state in (
            ThreadingTimeout.EXECUTED,
            ThreadingTimeout.EXECUTING,
            ThreadingTimeout.CANCELED,
        )

    def __repr__(self) -> str:
        return f"<{type(self).__name__} in state: {self.state}>"

    def __enter__(self) -> "ThreadingTimeout":
        self._target_tid = threading.get_ident()
        self.state = ThreadingTimeout.EXECUTING
        self._timer = threading.Timer(self.seconds, self._expire)
        # A timer left behind must not hold the interpreter open at exit.
        self._timer.daemon = True
        self._timer.start()
        return self

    def _expire(self) -> None:
        """Run on the timer thread when the time is up."""
        with self._lock:
            if self.state != ThreadingTimeout.EXECUTING or self._target_tid is None:
                return
            self.state = ThreadingTimeout.TIMED_OUT
            if _set_async_exc(self._target_tid, TimeoutException) > 1:
                # Never expected: undo rather than hit unrelated threads.
                _set_async_exc(self._target_tid, None)
                raise SystemError("PyThreadState_SetAsyncExc failed")

    def _withdraw_pending_timeout(self) -> None:
        """Make sure a timeout raised at this thread can't arrive later.

        Call with the lock held, from the block's own thread."""
        if self._target_tid is not None:
            _set_async_exc(self._target_tid, None)

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> bool:
        is_timeout = exc_type is not None and issubclass(exc_type, TimeoutException)
        try:
            with self._lock:
                if self._timer is not None:
                    self._timer.cancel()
                if self.state == ThreadingTimeout.TIMED_OUT:
                    if not is_timeout:
                        # Fired but not delivered (or caught inside the
                        # block): it must not surface after the block.
                        self._withdraw_pending_timeout()
                elif self.state == ThreadingTimeout.EXECUTING:
                    self.state = (
                        ThreadingTimeout.INTERRUPTED
                        if is_timeout
                        else ThreadingTimeout.EXECUTED
                    )
        except TimeoutException:
            # Our own timeout, reaching the thread here rather than being
            # withdrawn. The block is already over; there is nothing to stop.
            pass
        if is_timeout and self.state == ThreadingTimeout.TIMED_OUT:
            logger.debug(f"Block ran past its {self.seconds:g}s time limit")
            return self.swallow_exc
        return False

    def cancel(self) -> None:
        """Lift the time limit for the rest of the block."""
        try:
            with self._lock:
                if self._timer is not None:
                    self._timer.cancel()
                if self.state == ThreadingTimeout.TIMED_OUT:
                    self._withdraw_pending_timeout()
                self.state = ThreadingTimeout.CANCELED
        except TimeoutException:
            # Our own timeout, arriving just as it was lifted.
            self.state = ThreadingTimeout.CANCELED
