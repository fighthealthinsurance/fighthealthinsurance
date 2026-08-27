"""Tests for the staff-only, read-only Temporal Web UI reverse proxy."""

from unittest import mock

import requests
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

User = get_user_model()

_UPSTREAM_CALL = "fighthealthinsurance.staff_views._temporal_ui_request"


class _FakeUpstream:
    """Minimal stand-in for a requests.Response in stream mode."""

    def __init__(self, status=200, body=b"<html>ui</html>", headers=None):
        self.status_code = status
        self._body = body
        self.headers = {"Content-Type": "text/html; charset=utf-8", **(headers or {})}
        self.closed = False

    def iter_content(self, chunk_size=None):
        yield self._body
        yield b"<!-- tail -->"

    def close(self):
        self.closed = True


class TemporalUIProxyAccessTest(TestCase):
    def test_anonymous_is_redirected_to_login(self):
        response = self.client.get(reverse("temporal_ui", kwargs={"path": ""}))
        self.assertEqual(response.status_code, 302)

    def test_non_staff_is_redirected(self):
        User.objects.create_user(username="plain", password="pw123", is_staff=False)
        self.client.login(username="plain", password="pw123")
        response = self.client.get(reverse("temporal_ui", kwargs={"path": ""}))
        self.assertEqual(response.status_code, 302)


@override_settings(TEMPORAL_UI_UPSTREAM="http://temporal-web.test:8080")
class TemporalUIProxyStaffTest(TestCase):
    def setUp(self):
        User.objects.create_user(username="staff", password="pw123", is_staff=True)
        self.client.login(username="staff", password="pw123")

    @mock.patch(_UPSTREAM_CALL)
    def test_forwards_get_under_public_path_and_streams_body(self, upstream):
        upstream.return_value = _FakeUpstream(
            headers={
                "Cache-Control": "no-cache",
                "Content-Length": "999",
                "Connection": "keep-alive",
            }
        )
        response = self.client.get(
            reverse("temporal_ui", kwargs={"path": "workflows"}) + "?namespace=default"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            b"".join(response.streaming_content), b"<html>ui</html><!-- tail -->"
        )
        self.assertEqual(response["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(response["Cache-Control"], "no-cache")
        # Hop-by-hop / length headers are not copied through a re-streamed body.
        self.assertNotIn("Connection", response)
        self.assertNotIn("Content-Length", response)
        method, url = upstream.call_args.args
        self.assertEqual(method, "GET")
        self.assertEqual(
            url,
            "http://temporal-web.test:8080/timbit/temporal/workflows?namespace=default",
        )
        self.assertEqual(
            upstream.call_args.kwargs["headers"]["Accept-Encoding"], "identity"
        )
        self.assertFalse(upstream.call_args.kwargs["allow_redirects"])

    @mock.patch(_UPSTREAM_CALL)
    def test_root_without_slash_redirects_to_slash(self, upstream):
        response = self.client.get(reverse("temporal_ui_root"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/timbit/temporal/")
        upstream.assert_not_called()

    @mock.patch(_UPSTREAM_CALL)
    def test_only_get_and_head_pass_through(self, upstream):
        response = self.client.post(
            reverse("temporal_ui", kwargs={"path": "api/v1/workflows"})
        )
        self.assertEqual(response.status_code, 405)
        upstream.assert_not_called()

    @mock.patch(_UPSTREAM_CALL)
    def test_path_traversal_is_rejected(self, upstream):
        response = self.client.get("/timbit/temporal/../../timbit/admin/")
        self.assertIn(response.status_code, (400, 404))
        upstream.assert_not_called()

    @mock.patch(_UPSTREAM_CALL, side_effect=requests.ConnectionError("boom"))
    def test_unreachable_upstream_is_a_502_not_a_crash(self, upstream):
        response = self.client.get(reverse("temporal_ui", kwargs={"path": ""}))
        self.assertEqual(response.status_code, 502)
        self.assertIn(b"not reachable", response.content)

    @mock.patch(_UPSTREAM_CALL)
    def test_upstream_is_closed_when_the_client_stops_reading(self, upstream):
        fake = _FakeUpstream()
        upstream.return_value = fake
        response = self.client.get(reverse("temporal_ui", kwargs={"path": ""}))
        chunks = iter(response.streaming_content)
        next(chunks)  # read one chunk of two, then walk away
        self.assertFalse(fake.closed)
        response.close()  # what Django does when the connection ends
        self.assertTrue(fake.closed)

    @mock.patch(_UPSTREAM_CALL)
    def test_upstream_is_closed_after_full_read(self, upstream):
        fake = _FakeUpstream()
        upstream.return_value = fake
        response = self.client.get(reverse("temporal_ui", kwargs={"path": ""}))
        b"".join(response.streaming_content)
        self.assertTrue(fake.closed)

    @mock.patch(_UPSTREAM_CALL)
    def test_upstream_redirect_is_rewritten_to_our_side(self, upstream):
        upstream.return_value = _FakeUpstream(
            status=302,
            body=b"",
            headers={
                "Location": "http://temporal-web.test:8080/timbit/temporal/namespaces/default"
            },
        )
        response = self.client.get(reverse("temporal_ui", kwargs={"path": ""}))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/timbit/temporal/namespaces/default")
