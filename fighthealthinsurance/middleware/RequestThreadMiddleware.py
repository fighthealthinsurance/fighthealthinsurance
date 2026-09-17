"""Stamp each request with the thread Django's per-request cleanup runs on.

Under ASGI, Django runs the request's middleware and view on one
thread-sensitive thread per request, and its request_started and
request_finished receivers, ``close_old_connections`` among them, on that
same thread. It hands an exception raised before the view, a routing 404
above all, to ``response_for_exception`` with ``thread_sensitive=False``: a
plain executor thread. Database connections are strictly per thread, so a
connection that the error page opens there is nobody's to close, and it
outlives the request. See ``fighthealthinsurance.error_views`` for what is
done with the stamp.

``process_request`` on a ``MiddlewareMixin`` is exactly the hook that runs on
the thread-sensitive thread in an async chain, so the identity recorded here
is the one the cleanup runs on.
"""

import threading

from django.utils.deprecation import MiddlewareMixin

REQUEST_THREAD_ATTR = "fhi_request_thread"


class RequestThreadMiddleware(MiddlewareMixin):
    def process_request(self, request):
        setattr(request, REQUEST_THREAD_ATTR, threading.get_ident())
        return None
