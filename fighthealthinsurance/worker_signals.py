"""Bootstrap-time SIGTERM guard for the Temporal worker process.

No Django imports on purpose, and it must stay importable before anything
else in the package: manage.py installs this before it imports the rest of
the app, because in the container the worker is PID 1 and a PID 1 with no
handler for a signal never receives the kernel's default action. Without a
handler, a SIGTERM that lands anywhere between process start and the event
loop's own handlers (module imports, Django setup and checks, the command's
lazy imports) is discarded outright, and the pod goes on to poll its task
queue on a stale image until SIGKILL ends the grace period (seen in prod
2026-09-11). The command's event loop later installs its own handlers and
honours the flag set here.
"""

import signal
from typing import Optional


class Flag:
    """A set-once boolean with the Event interface, but no lock: a Python
    signal handler must not touch synchronization primitives, because a
    second signal re-entering the handler while the first holds the
    Event's non-reentrant lock deadlocks the main thread (review)."""

    __slots__ = ("value",)

    def __init__(self) -> None:
        self.value = False

    def set(self) -> None:
        self.value = True

    def is_set(self) -> bool:
        return self.value


class EarlyStop:
    """One SIGTERM flag per invocation.

    ``install()`` is idempotent so manage.py and the command's ``handle()``
    share the same flag; ``restore()`` puts the previous disposition back and
    hands the next invocation a fresh flag, so an early stop in one
    invocation cannot silently blank a later one in the same interpreter.
    """

    def __init__(self) -> None:
        self.event = Flag()
        self._previous: Optional[object] = None
        self._installed = False

    def _handler(self, signum, frame) -> None:
        self.event.set()

    def install(self) -> Flag:
        if not self._installed:
            try:
                previous = signal.getsignal(signal.SIGTERM)
                if previous is None:
                    # Installed outside Python (embedded interpreter): we
                    # could not hand it back, so do not take it (review).
                    return self.event
                signal.signal(signal.SIGTERM, self._handler)
                self._previous = previous
                self._installed = True
            except (ValueError, OSError):
                # Not the main thread: leave the default disposition.
                pass
        return self.event

    def restore(self) -> None:
        if self._installed:
            signal.signal(signal.SIGTERM, self._previous)  # type: ignore[arg-type]
            self._installed = False
        self.event = Flag()


early_stop = EarlyStop()
