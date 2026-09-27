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
finish or fail on its own.
"""

import asyncio
import threading
from typing import Any, Callable, Dict, Optional, TypeVar

T = TypeVar("T")

_lock = threading.Lock()
_running: Dict[str, int] = {}


def running(name: str) -> int:
    """How many threads started under ``name`` have not finished yet,
    including any whose caller already stopped waiting."""
    with _lock:
        return _running.get(name, 0)


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
    fn: Callable[..., T], *args: Any, timeout: float, name: str
) -> T:
    """Run the sync ``fn(*args)`` on a thread of its own, as described
    above, and return its result.

    Raises TimeoutError when it takes longer than ``timeout`` seconds, and
    whatever ``fn`` raised otherwise. A cancelled caller stops waiting the
    same way; the thread carries on until its statements finish or time out.
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

    _count(name, 1)
    try:
        threading.Thread(target=work, name=name, daemon=True).start()
    except BaseException:
        _count(name, -1)
        raise
    # Shielded, so giving up on the wait leaves the future alone; the
    # thread's work carries on either way.
    return await asyncio.wait_for(asyncio.shield(done), timeout)
