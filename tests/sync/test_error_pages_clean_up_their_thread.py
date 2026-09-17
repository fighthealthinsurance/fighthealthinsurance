"""Error pages rendered off the request's thread close the connection they open.

Under ASGI, Django hands a routing 404 to ``response_for_exception`` on a
plain executor thread; connections are per thread and the per-request
cleanup never runs there, so the connection the 404 page opens outlived the
request and, once the server reaped it, every later 404 on that thread died
with "the connection is closed". The wrapped error views run
``close_old_connections`` before and after rendering when they are off the
request's thread, and leave everything alone when they are on it.
"""

import inspect
import threading
from unittest.mock import patch

from django.conf import settings
from django.core.handlers import exception as django_exception_handlers
from django.http import Http404, HttpResponse
from django.test import RequestFactory, TestCase

from fighthealthinsurance import error_views, urls
from fighthealthinsurance.middleware import RequestThreadMiddleware
from fighthealthinsurance.middleware.RequestThreadMiddleware import (
    REQUEST_THREAD_ATTR,
)


class ThePremiseStillHoldsTest(TestCase):
    """If Django ever moves the exception handler onto the request's thread,
    this wrapper is dead weight and this test says so."""

    def test_django_renders_exceptions_off_the_thread_sensitive_thread(self):
        source = inspect.getsource(
            django_exception_handlers.convert_exception_to_response
        )
        self.assertIn("response_for_exception, thread_sensitive=False", source)


class RequestThreadMiddlewareTest(TestCase):
    def test_stamps_the_thread_it_runs_on(self):
        request = RequestFactory().get("/")

        RequestThreadMiddleware(lambda r: HttpResponse()).process_request(request)

        self.assertEqual(getattr(request, REQUEST_THREAD_ATTR), threading.get_ident())

    def test_runs_first(self):
        self.assertEqual(
            settings.MIDDLEWARE[0],
            "fighthealthinsurance.middleware.RequestThreadMiddleware",
        )


class ErrorPagesOffTheRequestThreadTest(TestCase):
    def setUp(self):
        self.request = RequestFactory().get("/nowhere")
        self.request.session = {}

    def _calls(self, stamp):
        """What the wrapper does around a render, as a sequence."""
        seen = []
        setattr(self.request, REQUEST_THREAD_ATTR, stamp)
        wrapped = error_views.cleaning_up_a_foreign_thread(
            lambda request, exception: seen.append("render") or HttpResponse()
        )
        with patch.object(
            error_views,
            "close_old_connections",
            side_effect=lambda: seen.append("close"),
        ):
            wrapped(self.request, exception=Http404())
        return seen

    def test_on_the_request_thread_nothing_is_closed(self):
        self.assertEqual(self._calls(threading.get_ident()), ["render"])

    def test_unstamped_counts_as_on_the_request_thread(self):
        seen = []
        wrapped = error_views.cleaning_up_a_foreign_thread(
            lambda request, exception: seen.append("render") or HttpResponse()
        )
        with patch.object(
            error_views,
            "close_old_connections",
            side_effect=lambda: seen.append("close"),
        ):
            wrapped(self.request, exception=Http404())
        self.assertEqual(seen, ["render"])

    def test_off_the_request_thread_closes_before_and_after(self):
        self.assertEqual(
            self._calls(threading.get_ident() + 1), ["close", "render", "close"]
        )

    def test_closes_after_even_when_the_render_raises(self):
        seen = []
        setattr(self.request, REQUEST_THREAD_ATTR, threading.get_ident() + 1)

        def failing(request, exception):
            seen.append("render")
            raise RuntimeError("template blew up")

        wrapped = error_views.cleaning_up_a_foreign_thread(failing)
        with patch.object(
            error_views,
            "close_old_connections",
            side_effect=lambda: seen.append("close"),
        ):
            with self.assertRaises(RuntimeError):
                wrapped(self.request, exception=Http404())
        self.assertEqual(seen, ["close", "render", "close"])

    def test_the_real_404_view_still_renders_the_page(self):
        setattr(self.request, REQUEST_THREAD_ATTR, threading.get_ident() + 1)
        with patch.object(error_views, "close_old_connections") as close:
            response = error_views.page_not_found(self.request, exception=Http404())
        self.assertEqual(response.status_code, 404)
        self.assertEqual(close.call_count, 2)


class TheHandlersAreWiredTest(TestCase):
    def test_urls_name_the_wrapped_views(self):
        self.assertEqual(
            urls.handler404, "fighthealthinsurance.error_views.page_not_found"
        )
        self.assertEqual(
            urls.handler403, "fighthealthinsurance.error_views.permission_denied"
        )
        self.assertEqual(
            urls.handler400, "fighthealthinsurance.error_views.bad_request"
        )

    def test_an_unknown_url_renders_the_404_page_through_the_client(self):
        response = self.client.get("/userfiles")
        self.assertEqual(response.status_code, 404)
        self.assertTemplateUsed(response, "404.html")

    def test_the_500_handler_is_wired_and_wrapped(self):
        self.assertEqual(
            urls.handler500, "fighthealthinsurance.error_views.server_error"
        )
        request = RequestFactory().get("/boom")
        setattr(request, REQUEST_THREAD_ATTR, threading.get_ident() + 1)
        with patch.object(error_views, "close_old_connections") as close:
            response = error_views.server_error(request)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(close.call_count, 2)

    def test_a_cleanup_that_raises_does_not_stop_the_page(self):
        request = RequestFactory().get("/nowhere")
        request.session = {}
        setattr(request, REQUEST_THREAD_ATTR, threading.get_ident() + 1)
        with patch.object(
            error_views, "close_old_connections", side_effect=RuntimeError("dead")
        ):
            response = error_views.page_not_found(request, exception=Http404())
        self.assertEqual(response.status_code, 404)
