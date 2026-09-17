"""Error pages that clean up after themselves on a thread that is not theirs.

Django's default 400, 403 and 404 views render their template with the
request, so every context processor runs, and ``form_persistence_context``
reads the session, which lives in the database. Under ASGI these views run
on a plain executor thread (``response_for_exception`` is called with
``thread_sensitive=False``), not the request's thread. Connections are per
thread, and ``close_old_connections`` only runs on the request's thread, so
the connection an error page opens on the executor thread is never closed.
It sits idle between error pages until the server's idle timeout or the
connection reaper kills it, and from then on every error page rendered on
that thread fails with "the connection is closed". Scanners hitting junk
URLs kept that going for a month.

So, when an error page finds itself off the request's thread, it runs the
same cleanup Django runs at request start and end: before rendering, so a
dead connection is replaced, and after, so none is left behind. On the
request's thread, WSGI and the test client included, nothing changes.

The 500 page is wrapped too. Today it renders without the request, so it
touches nothing, but a custom one that renders with the request would be
back in the same place. Django's response logging runs on that executor
thread as well; nothing in it touches the database, and nothing should.
"""

import threading
from functools import wraps
from typing import Callable

from django.db import close_old_connections
from django.views import defaults
from loguru import logger

from fighthealthinsurance.middleware.RequestThreadMiddleware import (
    REQUEST_THREAD_ATTR,
)


def on_the_request_thread(request) -> bool:
    """Whether this thread is the one the request's cleanup runs on.

    An unstamped request is treated as on-thread, so a request that never
    went through the middleware behaves exactly as before.
    """
    stamped = getattr(request, REQUEST_THREAD_ATTR, None)
    return stamped is None or stamped == threading.get_ident()


def _close_this_threads_connections(when: str) -> None:
    """Django's own per-request cleanup, for this thread's connections only.

    Never allowed to raise: the error page must render whatever state the
    connection is in, and closing a dead connection is best effort.
    """
    try:
        close_old_connections()
    except Exception as e:  # pragma: no cover - defensive, see docstring
        logger.opt(exception=True).warning(
            f"Closing this thread's database connections {when} an error page "
            f"failed: {e}"
        )


def cleaning_up_a_foreign_thread(render: Callable) -> Callable:
    @wraps(render)
    def handler(request, *args, **kwargs):
        if on_the_request_thread(request):
            return render(request, *args, **kwargs)
        _close_this_threads_connections("before")
        try:
            return render(request, *args, **kwargs)
        finally:
            _close_this_threads_connections("after")

    return handler


bad_request = cleaning_up_a_foreign_thread(defaults.bad_request)
permission_denied = cleaning_up_a_foreign_thread(defaults.permission_denied)
page_not_found = cleaning_up_a_foreign_thread(defaults.page_not_found)
server_error = cleaning_up_a_foreign_thread(defaults.server_error)
