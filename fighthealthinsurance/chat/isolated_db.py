"""Database work that must not share a chat's ORM executor.

Every other ORM call on the chat path goes through channels'
database_sync_to_async or the native async ORM (CLAUDE.md). Both run the
query on the connection's single thread-sensitive executor, which for a chat
is its WebSocket's own (websockets.PerConnectionThreadSensitiveMixin), and a
background task started from the chat inherits that executor. Cancelling an
await there does not stop a query that is already running, so a query stuck
behind a lock would hold the executor, and every later ORM call of the chat
would queue behind it.

``run_isolated`` is for work that must stay off that executor, such as a
background job's queries. It runs a sync function on a daemon thread of its
own, with that thread's own database connection, closed when the function
returns. The function runs inside a transaction, and on PostgreSQL every
statement in it is ended once it runs past the same bound the caller waits.
The caller stops waiting after ``timeout`` seconds and leaves the thread to
finish or fail on its own. A statement timeout does not end a thread waiting
on a connection that stopped answering, so a caller that must bound how many
of its threads run at once passes ``limit``.
"""

import asyncio
import threading
from typing import Any, Callable, Dict, Optional, TypeVar

T = TypeVar("T")

_lock = threading.Lock()
_running: Dict[str, int] = {}


def running(name: str) -> int:
    """How many threads started under ``name`` have not finished yet,
    including any whose caller already stopped waiting, plus the places
    reserved for later ones (reserve)."""
    with _lock:
        return _running.get(name, 0)


class Busy(Exception):
    """``limit`` threads under the name are still running."""


def _reserve(name: str, limit: Optional[int]) -> bool:
    """Count one more thread under ``name``, unless ``limit`` are already
    running. One step under the lock, so concurrent callers cannot all pass
    the check before any of them is counted."""
    with _lock:
        now = _running.get(name, 0)
        if limit is not None and now >= limit:
            return False
        _running[name] = now + 1
        return True


class Slot:
    """One place under a name's limit, reserved ahead of the work that will
    use it, so work that must run later cannot then be refused.
    run_isolated(slot=...) starts its thread in the place, and the thread
    gives it back when it ends; release() gives back a place that was never
    used. Either way it is given back once."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._spent = False
        self._spent_lock = threading.Lock()

    def _spend(self) -> bool:
        with self._spent_lock:
            if self._spent:
                return False
            self._spent = True
            return True

    def release(self) -> None:
        """Give the place back, unless a thread was started in it."""
        if self._spend():
            _count(self.name, -1)


def reserve(name: str, limit: int) -> Optional[Slot]:
    """A place under ``name`` for a thread to start later, counted against
    ``limit`` like a running thread, or None when ``limit`` places are
    already taken."""
    if not _reserve(name, limit):
        return None
    return Slot(name)


def _count(name: str, step: int) -> None:
    with _lock:
        now = _running.get(name, 0) + step
        if now > 0:
            _running[name] = now
        else:
            _running.pop(name, None)


def bound_statements(connection: Any, ms: int) -> None:
    """On PostgreSQL, end any statement of the current transaction that runs
    past ``ms`` (waiting on a lock included). Nothing elsewhere: sqlite in
    tests and development has no statement timeout."""
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        # set_config(..., true) is SET LOCAL: it ends with the transaction.
        cursor.execute("SELECT set_config('statement_timeout', %s, true)", [str(ms)])


def _call_in_transaction(fn: Callable[..., T], args: tuple, ms: int) -> T:
    """Run ``fn`` on this thread's own connection, inside a transaction
    bounded by a statement timeout, then close this thread's connections
    (it is a new thread each time, so nothing else would)."""
    from django.db import connection, connections, transaction

    try:
        with transaction.atomic():
            bound_statements(connection, ms)
            return fn(*args)
    finally:
        connections.close_all()


def _retrieve(future: "asyncio.Future[Any]") -> None:
    # A result nobody awaits any more (the caller timed out) must not be
    # reported as an exception that was never retrieved.
    if not future.cancelled():
        future.exception()


async def run_isolated(
    fn: Callable[..., T],
    *args: Any,
    timeout: float,
    name: str,
    limit: Optional[int] = None,
    slot: Optional[Slot] = None,
) -> T:
    """Run the sync ``fn(*args)`` on a thread of its own, as described
    above, and return its result.

    Raises TimeoutError when it takes longer than ``timeout`` seconds, and
    whatever ``fn`` raised otherwise. A cancelled caller stops waiting the
    same way; the thread carries on until its statements finish or time out.
    With ``limit``, raises Busy without starting a thread while ``limit``
    threads under ``name`` are still running, those whose callers stopped
    waiting included. With ``slot`` (from reserve, for the same name), the
    thread starts in that reserved place instead, and is never refused.
    """
    loop = asyncio.get_running_loop()
    done: "asyncio.Future[T]" = loop.create_future()
    done.add_done_callback(_retrieve)
    ms = max(1, int(timeout * 1000))

    def settle(result: Optional[T], error: Optional[BaseException]) -> None:
        if done.done():
            return
        if error is not None:
            done.set_exception(error)
        else:
            done.set_result(result)  # type: ignore[arg-type]

    def work() -> None:
        result: Optional[T] = None
        error: Optional[BaseException] = None
        try:
            result = _call_in_transaction(fn, args, ms)
        except Exception as e:
            error = e
        finally:
            _count(name, -1)
        try:
            loop.call_soon_threadsafe(settle, result, error)
        except RuntimeError:
            # The event loop closed while the work ran: nobody is waiting.
            pass

    if slot is not None:
        if slot.name != name or not slot._spend():
            raise ValueError("slot already used, or reserved for another name")
    elif not _reserve(name, limit):
        raise Busy()
    try:
        threading.Thread(target=work, name=name, daemon=True).start()
    except BaseException:
        _count(name, -1)
        raise
    # Shielded, so giving up on the wait leaves the future alone; the
    # thread's work carries on either way.
    return await asyncio.wait_for(asyncio.shield(done), timeout)
