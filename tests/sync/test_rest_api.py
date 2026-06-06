"""Test the rest API functionality"""

from asgiref.sync import async_to_sync, sync_to_async
from unittest.mock import patch

import asyncio

import pytest
from channels.testing import WebsocketCommunicator

import typing

import hashlib
import json

from django.urls import reverse
from django.contrib.auth import get_user_model
from django.utils import timezone
from dateutil.relativedelta import relativedelta


from rest_framework import status
from rest_framework.test import APITestCase

from fighthealthinsurance.models import (
    Denial,
    UserDomain,
    ExtraUserProperties,
    ProfessionalUser,
    Appeal,
    PatientUser,
    SecondaryAppealProfessionalRelation,
)
from fighthealthinsurance.websockets import (
    StreamingEntityBackend,
    StreamingAppealsBackend,
)
from fhi_users.models import (
    PatientDomainRelation,
    ProfessionalDomainRelation,
)

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
    from django.http import JsonResponse
else:
    User = get_user_model()


class DenialLongEmployerName(APITestCase):
    """Test denial with long employer name."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        self.domain = UserDomain.objects.create(
            name="testdomain",
            visible_phone_number="1234567890",
            internal_phone_number="0987654321",
            active=True,
            display_name="Test Domain",
            business_name="Test Business",
            country="USA",
            state="CA",
            city="Test City",
            address1="123 Test St",
            zipcode="12345",
        )
        self.user = User.objects.create_user(
            username=f"testuser🐼{self.domain.id}",
            password="testpass",
            email="test@example.com",
        )
        self.username = f"testuser🐼{self.domain.id}"
        self.password = "testpass"
        self.prouser = ProfessionalUser.objects.create(
            user=self.user, active=True, npi_number="1234567890"
        )
        self.user.is_active = True
        self.user.save()
        ExtraUserProperties.objects.create(user=self.user, email_verified=True)

    def test_long_employer_name(self):
        # Now we need to log in
        login_result = self.client.login(username=self.username, password=self.password)
        denial_text = "Group Name: "
        for a in range(0, 300):
            denial_text += str(a)
        denial_text += "INC "
        url = reverse("denials-list")
        email = "timbit@fighthealthinsurance.com"
        hashed_email = hashlib.sha512(email.encode("utf-8")).hexdigest()
        denials_for_user_count = Denial.objects.filter(
            hashed_email=hashed_email
        ).count()
        assert denials_for_user_count == 0
        # Create a denial
        response = self.client.post(
            url,
            json.dumps(
                {
                    "email": email,
                    "denial_text": denial_text,
                    "pii": "true",
                    "tos": "true",
                    "privacy": "true",
                    "store_raw_email": "true",  # Store the raw e-mail for the follow-up form
                }
            ),
            content_type="application/json",
        )
        self.assertTrue(status.is_success(response.status_code))
        denials_for_user_count = Denial.objects.filter(
            hashed_email=hashed_email,
        ).count()
        assert denials_for_user_count == 1


from typing import Dict, Any
from django.http import JsonResponse


class DenialEndToEnd(APITestCase):
    """Test end to end, we need to load the initial fixtures so we have denial types."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        self.domain = UserDomain.objects.create(
            name="testdomain",
            visible_phone_number="1234567890",
            internal_phone_number="0987654321",
            active=True,
            display_name="Test Domain",
            business_name="Test Business",
            country="USA",
            state="CA",
            city="Test City",
            address1="123 Test St",
            zipcode="12345",
        )
        self.user = User.objects.create_user(
            username=f"testuser🐼{self.domain.id}",
            password="testpass",
            email="test@example.com",
        )
        self.username = f"testuser🐼{self.domain.id}"
        self.password = "testpass"
        self.prouser = ProfessionalUser.objects.create(
            user=self.user, active=True, npi_number="1234567890"
        )
        self.user.is_active = True
        self.user.save()
        ExtraUserProperties.objects.create(user=self.user, email_verified=True)

    @pytest.mark.asyncio
    # Testing end to end for professional user
    async def test_denial_end_to_end(self):
        login_result = await sync_to_async(self.client.login)(
            username=self.username, password=self.password
        )
        self.assertTrue(login_result)
        url = reverse("denials-list")
        email = "timbit@fighthealthinsurance.com"
        hashed_email = Denial.get_hashed_email(email)
        denials_for_user_count = await Denial.objects.filter(
            hashed_email=hashed_email
        ).acount()
        assert denials_for_user_count == 0
        # Create a denial
        response = await sync_to_async(self.client.post)(
            url,
            json.dumps(
                {
                    "email": email,
                    "denial_text": "test",
                    "pii": "true",
                    "tos": "true",
                    "privacy": "true",
                    "store_raw_email": "true",  # Store the raw e-mail for the follow-up form
                }
            ),
            content_type="application/json",
        )
        self.assertTrue(status.is_success(response.status_code))
        parsed: Dict[str, Any] = response.json()
        denial_id = parsed["denial_id"]
        print(f"Using '{denial_id}'")
        semi_sekret = parsed["semi_sekret"]
        # Make sure we added a denial for this user
        denials_for_user_count = await Denial.objects.filter(
            hashed_email=hashed_email,
        ).acount()
        assert denials_for_user_count > 0
        # Make sure we can get the denial
        denial = await Denial.objects.filter(
            hashed_email=hashed_email, denial_id=denial_id
        ).aget()
        print(f"We should find {denial}")

        # Now we need to poke entity extraction, this part is async.
        # Mock fire_and_forget_in_new_threadpool to avoid background threads
        # that race with test teardown.
        async def _noop_fire_and_forget(task):
            task.close()  # Prevent "coroutine was never awaited" warning

        with patch(
            "fighthealthinsurance.common_view_logic.fire_and_forget_in_new_threadpool",
            side_effect=_noop_fire_and_forget,
        ):
            seb_communicator = WebsocketCommunicator(
                StreamingEntityBackend.as_asgi(), "/testws/"
            )
            connected, _ = await seb_communicator.connect()
            assert connected
            await seb_communicator.send_json_to(
                {
                    "email": email,
                    "semi_sekret": semi_sekret,
                    "denial_id": denial_id,
                }
            )
            # We should receive at least one frame.
            response = await seb_communicator.receive_from(timeout=30)
            # Now consume all of the rest of them until done.
            try:
                while True:
                    response = await seb_communicator.receive_from(timeout=30)
            except (asyncio.TimeoutError, AssertionError):
                # TimeoutError: no more data within timeout
                # AssertionError: websocket.close received (connection closed by server)
                pass
        # Set health history before next steps
        health_history_url = reverse("healthhistory-list")
        health_history_response = await sync_to_async(self.client.post)(
            health_history_url,
            json.dumps(
                {
                    "denial_id": denial_id,
                    "email": email,
                    "semi_sekret": semi_sekret,
                    "health_history": "Sample health history",
                }
            ),
            content_type="application/json",
        )
        self.assertTrue(status.is_success(health_history_response.status_code))
        # Ok now lets get the additional info
        find_next_steps_url = reverse("nextsteps-list")
        find_next_steps_response: JsonResponse = await sync_to_async(self.client.post)(
            find_next_steps_url,
            json.dumps(
                {
                    "email": email,
                    "semi_sekret": semi_sekret,
                    "denial_id": denial_id,
                    "denial_type": [1, 2],
                    "diagnosis": "high risk homosexual behaviour",
                    "include_provided_health_history_in_appeal": True,
                }
            ),
            content_type="application/json",
        )
        find_next_steps_parsed: Dict[str, Any] = find_next_steps_response.json()
        # Make sure we got back a reasonable set of questions. Reduced to 4 since in_network is handled separately for professionals
        assert len(find_next_steps_parsed["combined_form"]) >= 4
        assert list(find_next_steps_parsed["combined_form"][0].keys()) == [
            "name",
            "field_type",
            "label",
            "visible",
            "required",
            "help_text",
            "initial",
            "type",
        ]
        # Verify include_provided_health_history is set on the denial
        denial = await Denial.objects.aget(denial_id=denial_id)
        assert denial.include_provided_health_history_in_appeal is True
        # Now we need to poke at the appeal creator
        # Now we need to poke entity extraction, this part is async
        a_communicator = WebsocketCommunicator(
            StreamingAppealsBackend.as_asgi(), "/testws/"
        )
        connected, _ = await a_communicator.connect()
        assert connected
        await a_communicator.send_json_to(
            {
                "email": email,
                "semi_sekret": semi_sekret,
                "medical_reason": "preventive",
                "age": "30",
                "in_network": True,
                "denial_id": denial_id,
            }
        )
        responses = []
        # We should receive at least one frame.
        response = await a_communicator.receive_from(timeout=300)
        print(f"Received response {response}")
        responses.append(response)
        # Now consume all of the rest of them until done.
        try:
            while True:
                response = await a_communicator.receive_from(timeout=150)
                print(f"Received response {response}")
                responses.append(response)
        except Exception as e:
            print(f"Error {e}")
            pass
        print(f"Received responses {responses}")
        # Find just the appeals ones, quick hack look for the "content" string to avoid full parse.
        responses = list(
            filter(lambda x: '"content"' in x, filter(lambda x: len(x) > 4, responses))
        )
        # It's a streaming response with one per new line
        appeal = json.loads(responses[0])
        assert appeal["content"].lstrip().startswith("Dear")
        # Now lets go ahead and provide follow up
        denial = await Denial.objects.aget(denial_id=denial_id)
        followup_url = reverse("followups-list")
        followup_response = await sync_to_async(self.client.post)(
            followup_url,
            json.dumps(
                {
                    "denial_id": denial_id,
                    "uuid": str(denial.uuid),
                    "hashed_email": denial.hashed_email,
                    "user_comments": "test",
                    "appeal_result": "Yes",
                    "follow_up_again": True,
                    "follow_up_semi_sekret": denial.follow_up_semi_sekret,
                }
            ),
            content_type="application/json",
        )
        print(followup_response)
        self.assertTrue(status.is_success(followup_response.status_code))

    @pytest.mark.asyncio
    async def test_appeal_generation_status_messages(self):
        """Test that appeal generation WebSocket sends status messages."""
        # Setup: Create a denial through the API
        login_result = await sync_to_async(self.client.login)(
            username=self.username, password=self.password
        )
        self.assertTrue(login_result)
        url = reverse("denials-list")
        email = "test@example.com"
        response = await sync_to_async(self.client.post)(
            url,
            json.dumps(
                {
                    "denial_text": "Your claim has been denied because the requested treatment is not medically necessary.",
                    "denial_type": "1",
                    "plan_id": "",
                    "claim_id": "",
                    "state": "CA",
                    "email": email,
                    "pii": True,
                    "tos": True,
                    "privacy": True,
                }
            ),
            content_type="application/json",
        )
        self.assertTrue(status.is_success(response.status_code))
        data = response.json()
        denial_id = data["denial_id"]
        semi_sekret = data["semi_sekret"]

        # Run entity extraction first
        seb_communicator = WebsocketCommunicator(
            StreamingEntityBackend.as_asgi(), "/testws/"
        )
        connected, _ = await seb_communicator.connect()
        assert connected
        await seb_communicator.send_json_to(
            {
                "email": email,
                "semi_sekret": semi_sekret,
                "denial_id": denial_id,
            }
        )
        # Consume entity extraction responses
        try:
            while True:
                response = await seb_communicator.receive_from(timeout=30)
        except Exception:
            pass

        # Now test appeal generation with status messages
        a_communicator = WebsocketCommunicator(
            StreamingAppealsBackend.as_asgi(), "/testws/"
        )
        connected, _ = await a_communicator.connect()
        assert connected
        await a_communicator.send_json_to(
            {
                "email": email,
                "semi_sekret": semi_sekret,
                "medical_reason": "preventive",
                "age": "30",
                "in_network": True,
                "denial_id": denial_id,
            }
        )

        # Collect all responses
        responses = []
        status_messages = []
        appeal_contents = []

        try:
            while True:
                response = await a_communicator.receive_from(timeout=150)
                try:
                    # Skip empty keep alive lines
                    if len(response) < 2:
                        continue
                    responses.append(response)
                    parsed = json.loads(response)
                    if parsed.get("type") == "status":
                        status_messages.append(parsed.get("message"))
                    elif "content" in parsed:
                        appeal_contents.append(parsed["content"])
                    else:
                        print(f"Got a non-status message without content? {parsed}")
                except json.JSONDecodeError:
                    pass  # Skip non-JSON lines
        except Exception as e:
            print(f"Done receiving: {e}")
            pass

        # Verify we received status messages
        print(f"Status messages received: {status_messages}")
        print(f"Appeals received: {len(appeal_contents)}")

        # Assert we got at least some expected status messages
        assert len(status_messages) > 0, "Should have received status messages"

        # Check for expected status messages
        status_text = " ".join(status_messages).lower()
        assert any(
            keyword in status_text
            for keyword in ["starting", "loading", "generating", "gathering"]
        ), f"Expected status keywords not found in: {status_messages}"

        # Verify we still get appeal content
        assert (
            len(appeal_contents) >= 1
        ), f"Should have received at least one appeal in {responses}"
        assert (
            appeal_contents[0].lstrip().startswith("Dear")
        ), "Appeal should start with 'Dear'"


class StreamingAppealsRestFallbackTest(APITestCase):
    """Test the REST/HTTP fallback for the WebSocket appeals stream.

    Clients that can't hold a WebSocket open (iOS Safari ITP, corporate
    proxies blocking WS upgrades, captive portals) fall back to this
    endpoint after exhausting WebSocket retries.
    """

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    @staticmethod
    def _drain_streaming_response(response) -> str:
        """Drain a StreamingHttpResponse with an async iterator body.

        The view itself is sync but its StreamingHttpResponse wraps an
        async generator as `streaming_content`, so b"".join() can't
        consume it directly. Use async_to_sync rather than
        asyncio.get_event_loop() because the latter raises
        RuntimeError when a prior test in the suite has closed the
        default loop (Python 3.12+ behavior).
        """

        async def _drain(stream):
            chunks = []
            async for chunk in stream:
                chunks.append(
                    chunk.encode() if isinstance(chunk, str) else chunk
                )
            return b"".join(chunks)

        return async_to_sync(_drain)(response.streaming_content).decode()

    @staticmethod
    def _content_lines(body: str) -> list:
        return [
            line
            for line in body.split("\n")
            if line.strip() and '"content"' in line
        ]

    def _post_fallback(self, payload: dict):
        return self.client.post(
            reverse("streaming_appeals_fallback"),
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_invalid_json_returns_400(self):
        url = reverse("streaming_appeals_fallback")
        response = self.client.post(
            url, data="not-json", content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)
        # DRF's JSON parser raises ParseError -> default exception
        # handler renders {"detail": "JSON parse error - ..."}.
        body = response.content.decode().lower()
        self.assertTrue(
            "json" in body and ("parse" in body or "invalid" in body),
            f"Expected JSON parse error in body, got {body!r}",
        )

    def test_non_object_json_returns_400(self):
        url = reverse("streaming_appeals_fallback")
        # Valid JSON but not an object — must not crash with AttributeError
        # on data.get(...).
        response = self.client.post(
            url, data=json.dumps([1, 2, 3]), content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("JSON object", response.content.decode())

    def test_get_method_not_allowed(self):
        url = reverse("streaming_appeals_fallback")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 405)

    def test_streams_ndjson_with_anti_buffering_headers(self):
        """Mock generate_appeals so we can assert framing without
        invoking the real ML pipeline. A real denial is created so the
        upfront magic-key auth gate passes."""
        url = reverse("streaming_appeals_fallback")
        denial = self._make_real_denial(email="framing@example.com")

        async def fake_generate_appeals(_data):
            yield (
                json.dumps(
                    {"type": "status", "phase": "init", "message": "starting"}
                )
                + "\n"
            )
            yield json.dumps({"id": "1", "content": "Dear Insurer,..."}) + "\n"
            yield (
                json.dumps(
                    {
                        "type": "status",
                        "phase": "done",
                        "total_appeals": 1,
                        "new_appeals": 1,
                        "existing_appeals": 0,
                    }
                )
                + "\n"
            )

        from fighthealthinsurance.common_view_logic import AppealsBackendHelper

        # Patch on the class directly. `new=fake_generate_appeals`
        # replaces the classmethod descriptor with a plain async
        # generator function — when the view calls
        # `AppealsBackendHelper.generate_appeals(data)`, Python
        # invokes our function with just `(data,)`.
        # IMPORTANT: drain inside the patch context. StreamingHttpResponse
        # holds a lazy async iterator — body bytes are produced only when
        # consumed, so unpatching before drain lets the real code run.
        with patch.object(
            AppealsBackendHelper,
            "generate_appeals",
            new=fake_generate_appeals,
        ):
            response = self.client.post(
                url,
                data=json.dumps(
                    {
                        "denial_id": denial.denial_id,
                        "email": "framing@example.com",
                        "semi_sekret": denial.semi_sekret,
                    }
                ),
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response["Content-Type"], "application/x-ndjson"
            )
            # Anti-buffering headers — defeating these proxies is the
            # whole point of the fallback existing.
            self.assertEqual(response["X-Accel-Buffering"], "no")
            self.assertIn("no-cache", response["Cache-Control"])

            body = self._drain_streaming_response(response)
        # Expect at least the appeal frame and the done status frame
        appeal_lines = self._content_lines(body)
        done_lines = [
            line
            for line in body.split("\n")
            if line.strip() and '"phase": "done"' in line
        ]
        self.assertEqual(len(appeal_lines), 1)
        self.assertEqual(len(done_lines), 1)

    # ------------------------------------------------------------------
    # Magic-key auth tests
    # ------------------------------------------------------------------
    # The PR explicitly mandates that appeals never release on denial_id
    # alone — the (denial_id, email, semi_sekret) triple is the
    # canonical credential. The view validates the triple upfront and
    # returns 404 before invoking generation, so these tests assert
    # both the status code AND that the response body (which is now a
    # short JSON error rather than a stream) contains no appeal text.

    def _make_real_denial(self, email: str = "owner@example.com") -> Denial:
        """Create a Denial directly via ORM, mirroring how the denial
        creation API ultimately persists one. Tests use this so we can
        exercise auth paths without the full denial-creation flow."""
        return Denial.objects.create(
            denial_text="Test denial body",
            hashed_email=Denial.get_hashed_email(email),
        )

    def _assert_auth_failure(self, response) -> None:
        """Auth gate must produce a 4xx response with no appeal content
        in the body. Uniform 404 means we don't leak which field was
        wrong."""
        self.assertEqual(response.status_code, 404)
        body = response.content.decode()
        self.assertNotIn('"content"', body)

    def test_rejects_wrong_semi_sekret(self):
        denial = self._make_real_denial()
        response = self._post_fallback(
            {
                "denial_id": denial.denial_id,
                "email": "owner@example.com",
                "semi_sekret": "wrong-sekret-not-the-real-one",
            }
        )
        self._assert_auth_failure(response)

    def test_rejects_wrong_email(self):
        denial = self._make_real_denial()
        response = self._post_fallback(
            {
                "denial_id": denial.denial_id,
                "email": "different-user@example.com",
                "semi_sekret": denial.semi_sekret,
            }
        )
        self._assert_auth_failure(response)

    def test_rejects_missing_credentials(self):
        denial = self._make_real_denial()
        response = self._post_fallback({"denial_id": denial.denial_id})
        self._assert_auth_failure(response)

    def test_cross_session_isolation(self):
        """Denial A's id with Denial B's credentials must not leak A's
        appeals (or any appeals at all)."""
        denial_a = self._make_real_denial(email="alice@example.com")
        denial_b = self._make_real_denial(email="bob@example.com")
        response = self._post_fallback(
            {
                "denial_id": denial_a.denial_id,
                "email": "bob@example.com",
                "semi_sekret": denial_b.semi_sekret,
            }
        )
        self._assert_auth_failure(response)

    def test_rejects_bogus_denial_id(self):
        """Unknown denial_id with any credentials must 404 — and not
        invoke backend generation. Patching generate_appeals makes
        the 'no generation' guarantee executable: a regression that
        triggered generation before the 404 would fail this assert."""
        from fighthealthinsurance.common_view_logic import AppealsBackendHelper

        with patch.object(
            AppealsBackendHelper, "generate_appeals"
        ) as generate_appeals:
            response = self._post_fallback(
                {
                    "denial_id": 999999,
                    "email": "x@example.com",
                    "semi_sekret": "anything",
                }
            )
        self._assert_auth_failure(response)
        generate_appeals.assert_not_called()

    # ------------------------------------------------------------------
    # WS -> REST handoff dedup
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_ws_suppression_flag_yields_no_appeals(self):
        """With SUPPRESS_APPEAL_WS_DELIVERY=True, the WS must accept
        the connection and close without yielding any appeal payloads.
        This is the seam we use to drive the JS client's REST fallback
        path in tests without real WS failure injection."""
        from fighthealthinsurance import websockets

        denial = await sync_to_async(self._make_real_denial)()

        original = websockets.SUPPRESS_APPEAL_WS_DELIVERY
        websockets.SUPPRESS_APPEAL_WS_DELIVERY = True
        try:
            communicator = WebsocketCommunicator(
                StreamingAppealsBackend.as_asgi(), "/testws/"
            )
            connected, _ = await communicator.connect()
            assert connected
            await communicator.send_json_to(
                {
                    "denial_id": denial.denial_id,
                    "email": "owner@example.com",
                    "semi_sekret": denial.semi_sekret,
                }
            )
            # Consume frames until close. With suppression on, no
            # content frames should ever arrive.
            content_frames = 0
            try:
                while True:
                    frame = await communicator.receive_from(timeout=5)
                    if frame and '"content"' in frame:
                        content_frames += 1
            except (asyncio.TimeoutError, AssertionError):
                # AssertionError: server-side close
                pass
            finally:
                await communicator.disconnect()
        finally:
            websockets.SUPPRESS_APPEAL_WS_DELIVERY = original

        self.assertEqual(content_frames, 0)

    def test_handoff_emits_unique_appeal_ids(self):
        """After a suppressed WS attempt, the REST fallback must
        emit each appeal with a unique id. The client's dedup logic
        relies on this contract — duplicate ids would render the same
        appeal twice in the UI."""
        from fighthealthinsurance.common_view_logic import AppealsBackendHelper

        # Real denial so the upfront auth gate passes; generation is
        # then mocked to keep the test deterministic.
        denial = self._make_real_denial(email="handoff@example.com")

        async def fake_three_appeals(_data):
            for i in (1, 2, 3):
                yield (
                    json.dumps({"id": f"appeal-{i}", "content": f"Body {i}"})
                    + "\n"
                )
            yield (
                json.dumps(
                    {
                        "type": "status",
                        "phase": "done",
                        "total_appeals": 3,
                        "new_appeals": 3,
                        "existing_appeals": 0,
                    }
                )
                + "\n"
            )

        with patch.object(
            AppealsBackendHelper,
            "generate_appeals",
            new=fake_three_appeals,
        ):
            response = self._post_fallback(
                {
                    "denial_id": denial.denial_id,
                    "email": "handoff@example.com",
                    "semi_sekret": denial.semi_sekret,
                }
            )
            self.assertEqual(response.status_code, 200)
            body = self._drain_streaming_response(response)

        # Collect ids from every content-bearing frame
        ids = []
        for line in body.split("\n"):
            stripped = line.strip()
            if not stripped or '"content"' not in stripped:
                continue
            parsed = json.loads(stripped)
            ids.append(parsed["id"])

        self.assertEqual(len(ids), 3)
        # Dedup contract: every id is unique within a single stream
        self.assertEqual(len(set(ids)), len(ids))


class EnableExternalModelsTest(APITestCase):
    """Test the endpoint that lets users opt their denial into external
    LLM models from the appeal page when the initial internal-only
    generation produced few or no appeals."""

    def _make_denial(
        self,
        *,
        email: str = "owner@example.com",
        use_external: bool = False,
    ) -> Denial:
        return Denial.objects.create(
            denial_text="Test denial body",
            hashed_email=Denial.get_hashed_email(email),
            use_external=use_external,
        )

    def _post(self, payload: dict):
        return self.client.post(
            reverse("enable_external_models"),
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_flips_use_external_on_valid_credentials(self):
        denial = self._make_denial()
        self.assertFalse(denial.use_external)
        response = self._post(
            {
                "denial_id": denial.denial_id,
                "email": "owner@example.com",
                "semi_sekret": denial.semi_sekret,
            }
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"use_external": True})
        denial.refresh_from_db()
        self.assertTrue(denial.use_external)

    def test_idempotent_when_already_enabled(self):
        """Calling the endpoint when external models are already on
        must succeed (200) without raising. The button is only shown to
        users whose denial has use_external=False, but races and reloads
        should not 500."""
        denial = self._make_denial(
            email="already@example.com", use_external=True
        )
        response = self._post(
            {
                "denial_id": denial.denial_id,
                "email": "already@example.com",
                "semi_sekret": denial.semi_sekret,
            }
        )
        self.assertEqual(response.status_code, 200)
        denial.refresh_from_db()
        self.assertTrue(denial.use_external)

    def test_rejects_wrong_semi_sekret(self):
        denial = self._make_denial()
        response = self._post(
            {
                "denial_id": denial.denial_id,
                "email": "owner@example.com",
                "semi_sekret": "wrong-sekret",
            }
        )
        self.assertEqual(response.status_code, 404)
        denial.refresh_from_db()
        self.assertFalse(denial.use_external)

    def test_rejects_wrong_email(self):
        denial = self._make_denial()
        response = self._post(
            {
                "denial_id": denial.denial_id,
                "email": "someone-else@example.com",
                "semi_sekret": denial.semi_sekret,
            }
        )
        self.assertEqual(response.status_code, 404)
        denial.refresh_from_db()
        self.assertFalse(denial.use_external)

    def test_rejects_missing_credentials(self):
        denial = self._make_denial()
        response = self._post({"denial_id": denial.denial_id})
        self.assertEqual(response.status_code, 404)
        denial.refresh_from_db()
        self.assertFalse(denial.use_external)

    def test_rejects_non_object_body(self):
        """Non-mapping JSON bodies (arrays, strings, numbers) must 400
        rather than 500. Without the isinstance guard, request.data.get
        on a list would raise AttributeError."""
        response = self.client.post(
            reverse("enable_external_models"),
            data=json.dumps([1, 2, 3]),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("JSON object", response.content.decode())

    def test_cross_denial_isolation(self):
        """Denial A's credentials must not be able to flip Denial B."""
        denial_a = self._make_denial(email="alice@example.com")
        denial_b = self._make_denial(email="bob@example.com")
        response = self._post(
            {
                "denial_id": denial_b.denial_id,
                "email": "alice@example.com",
                "semi_sekret": denial_a.semi_sekret,
            }
        )
        self.assertEqual(response.status_code, 404)
        denial_b.refresh_from_db()
        self.assertFalse(denial_b.use_external)


class GenerateAppealUseExternalContextTest(APITestCase):
    """The appeals page must expose `use_external` so the JS client can
    decide whether to surface the 'request external models' opt-in."""

    def test_context_reflects_internal_only_denial(self):
        denial = Denial.objects.create(
            denial_text="Internal-only denial",
            hashed_email=Denial.get_hashed_email("internal@example.com"),
            use_external=False,
        )
        response = self.client.get(
            reverse("generate_appeal"),
            {
                "denial_id": str(denial.denial_id),
                "email": "internal@example.com",
                "semi_sekret": denial.semi_sekret,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["use_external"])
        # Prompt block (which wraps the button) only renders when
        # use_external is False.
        self.assertContains(response, "external-models-prompt")

    def test_context_reflects_external_enabled_denial(self):
        denial = Denial.objects.create(
            denial_text="External-enabled denial",
            hashed_email=Denial.get_hashed_email("external@example.com"),
            use_external=True,
        )
        response = self.client.get(
            reverse("generate_appeal"),
            {
                "denial_id": str(denial.denial_id),
                "email": "external@example.com",
                "semi_sekret": denial.semi_sekret,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["use_external"])
        # When external is already on, we don't render the opt-in prompt.
        self.assertNotContains(response, "external-models-prompt")


class NotifyPatientTest(APITestCase):
    """Test the notify_patient API endpoint."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        # Create domain
        self.domain = UserDomain.objects.create(
            name="testdomain",
            visible_phone_number="1234567890",
            internal_phone_number="0987654321",
            active=True,
            display_name="Test Domain",
            business_name="Test Business",
            country="USA",
            state="CA",
            city="Test City",
            address1="123 Test St",
            zipcode="12345",
        )

        # Create professional user
        self.pro_user = User.objects.create_user(
            username=f"prouser🐼{self.domain.id}",
            password="testpass",
            email="pro@example.com",
        )
        self.pro_username = f"prouser🐼{self.domain.id}"
        self.pro_password = "testpass"
        self.professional = ProfessionalUser.objects.create(
            user=self.pro_user, active=True, npi_number="1234567890"
        )
        self.pro_user.is_active = True
        self.pro_user.save()
        ExtraUserProperties.objects.create(user=self.pro_user, email_verified=True)

        # Create patient user
        self.patient_user = User.objects.create_user(
            username="patientuser",
            password="patientpass",
            email="patient@example.com",
            first_name="Test",
            last_name="Patient",
        )
        self.patient_user.is_active = True
        self.patient_user.save()
        self.patient = PatientUser.objects.create(user=self.patient_user)

        # Create a denial
        self.denial = Denial.objects.create(
            denial_text="Test denial",
            primary_professional=self.professional,
            creating_professional=self.professional,
            patient_user=self.patient,
            hashed_email=Denial.get_hashed_email(self.patient_user.email),
        )

        # Create an appeal
        self.appeal = Appeal.objects.create(
            for_denial=self.denial,
            pending=True,
            patient_user=self.patient,
            primary_professional=self.professional,
            creating_professional=self.professional,
        )

        # Set up session
        self.client.login(username=self.pro_username, password=self.pro_password)
        session = self.client.session
        session["domain_id"] = str(self.domain.id)
        session.save()

    def test_notify_patient(self):
        url = reverse("appeals-notify-patient")

        # Test with professional name included
        response = self.client.post(
            url,
            json.dumps({"id": self.appeal.id, "include_professional": True}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("message", response.json())
        self.assertEqual(response.json()["message"], "Notification sent")

        # Test without professional name
        response = self.client.post(
            url,
            json.dumps({"id": self.appeal.id, "include_professional": False}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("message", response.json())
        self.assertEqual(response.json()["message"], "Notification sent")

    def test_notify_patient_inactive_user(self):
        # Set patient user to inactive to test invitation flow
        self.patient_user.is_active = False
        self.patient_user.save()

        url = reverse("appeals-notify-patient")
        response = self.client.post(
            url,
            json.dumps({"id": self.appeal.id, "professional_name": True}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("message", response.json())
        self.assertEqual(response.json()["message"], "Notification sent")


class SendFaxTest(APITestCase):
    """Test the send_fax API endpoint."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        # Create domain
        self.domain = UserDomain.objects.create(
            name="testdomain",
            visible_phone_number="1234567890",
            internal_phone_number="0987654321",
            active=True,
            display_name="Test Domain",
            business_name="Test Business",
            country="USA",
            state="CA",
            city="Test City",
            address1="123 Test St",
            zipcode="12345",
        )

        # Create professional user
        self.pro_user = User.objects.create_user(
            username=f"prouser🐼{self.domain.id}",
            password="testpass",
            email="pro@example.com",
        )
        self.pro_username = f"prouser🐼{self.domain.id}"
        self.pro_password = "testpass"
        self.professional = ProfessionalUser.objects.create(
            user=self.pro_user, active=True, npi_number="1234567890"
        )
        self.pro_user.is_active = True
        self.pro_user.save()
        ExtraUserProperties.objects.create(user=self.pro_user, email_verified=True)

        # Create patient user
        self.patient_user = User.objects.create_user(
            username="patientuser",
            password="patientpass",
            email="patient@example.com",
            first_name="Test",
            last_name="Patient",
        )
        self.patient_user.is_active = True
        self.patient_user.save()
        self.patient = PatientUser.objects.create(
            user=self.patient_user,
            active=True,
        )

        # Create a denial with appeal text
        self.denial = Denial.objects.create(
            denial_text="Test denial",
            primary_professional=self.professional,
            creating_professional=self.professional,
            patient_user=self.patient,
            hashed_email=Denial.get_hashed_email(self.patient_user.email),
            appeal_fax_number="5551234567",
            patient_visible=True,
        )

        # Create an appeal with text
        self.appeal = Appeal.objects.create(
            for_denial=self.denial,
            pending=True,
            patient_user=self.patient,
            primary_professional=self.professional,
            creating_professional=self.professional,
            appeal_text="!This is a test appeal letter",
            patient_visible=True,
        )

        # Set up session for professional
        self.client.login(username=self.pro_username, password=self.pro_password)
        session = self.client.session
        session["domain_id"] = str(self.domain.id)
        session.save()

    def test_send_fax_as_professional(self):
        url = reverse("appeals-send-fax")

        response = self.client.post(
            url,
            json.dumps({"appeal_id": self.appeal.id, "fax_number": "5559876543"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        # Verify the fax number was updated
        updated_appeal = Appeal.objects.get(id=self.appeal.id)
        self.assertEqual(updated_appeal.pending, False)
        self.assertEqual(updated_appeal.pending_patient, False)
        self.assertEqual(updated_appeal.pending_professional, False)

    def test_send_fax_aspatient_no_permissions(self):
        # Login as patient
        self.client.logout()
        self.client.login(username="patientuser", password="patientpass")
        session = self.client.session
        session["domain_id"] = str(self.domain.id)
        session.save()

        # Set the appeal to require professional finishing
        self.denial.professional_to_finish = True
        self.denial.save()

        url = reverse("appeals-send-fax")

        response = self.client.post(
            url,
            json.dumps({"appeal_id": self.appeal.id, "fax_number": "5559876543"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("Pending", response.json()["message"])

        # Verify the pending flags were updated correctly
        self.appeal.refresh_from_db()
        self.assertEqual(self.appeal.pending, True)
        self.assertEqual(self.appeal.pending_patient, False)
        self.assertEqual(self.appeal.pending_professional, True)

    def test_send_fax_aspatient_with_permissions(self):
        # Login as patient
        self.client.logout()
        self.client.login(username="patientuser", password="patientpass")
        session = self.client.session
        session["domain_id"] = str(self.domain.id)
        session.save()

        # Set the appeal to allow the patient to finish
        self.denial.professional_to_finish = False
        self.denial.save()

        url = reverse("appeals-send-fax")

        response = self.client.post(
            url,
            json.dumps({"appeal_id": self.appeal.id, "fax_number": "5559876543"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.appeal.refresh_from_db()
        self.assertEqual(self.appeal.pending, False)


class InviteProviderTest(APITestCase):
    """Test the invite_provider API endpoint."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        # Create domain
        self.domain = UserDomain.objects.create(
            name="testdomain",
            visible_phone_number="1234567890",
            internal_phone_number="0987654321",
            active=True,
            display_name="Test Domain",
            business_name="Test Business",
            country="USA",
            state="CA",
            city="Test City",
            address1="123 Test St",
            zipcode="12345",
        )

        # Create primary professional user
        self.primary_pro_user = User.objects.create_user(
            username=f"primary_pro🐼{self.domain.id}",
            password="testpass",
            email="primary@example.com",
        )
        self.primary_pro_username = f"primary_pro🐼{self.domain.id}"
        self.primary_pro_password = "testpass"
        self.primary_professional = ProfessionalUser.objects.create(
            user=self.primary_pro_user, active=True, npi_number="1234567890"
        )
        self.primary_pro_user.is_active = True
        self.primary_pro_user.save()
        ExtraUserProperties.objects.create(
            user=self.primary_pro_user, email_verified=True
        )

        # Create secondary professional user
        self.secondary_pro_user = User.objects.create_user(
            username=f"secondary_pro🐼{self.domain.id}",
            password="testpass",
            email="secondary@example.com",
        )
        self.secondary_professional = ProfessionalUser.objects.create(
            user=self.secondary_pro_user, active=True, npi_number="0987654321"
        )
        self.secondary_pro_user.is_active = True
        self.secondary_pro_user.save()

        # Create patient user
        self.patient_user = User.objects.create_user(
            username="patientuser",
            password="patientpass",
            email="patient@example.com",
            first_name="Test",
            last_name="Patient",
        )
        self.patient_user.is_active = True
        self.patient_user.save()
        self.patient = PatientUser.objects.create(
            user=self.patient_user,
        )

        # Create a denial
        self.denial = Denial.objects.create(
            denial_text="Test denial",
            primary_professional=self.primary_professional,
            creating_professional=self.primary_professional,
            patient_user=self.patient,
            hashed_email=Denial.get_hashed_email(self.patient_user.email),
        )

        # Create an appeal
        self.appeal = Appeal.objects.create(
            for_denial=self.denial,
            pending=True,
            patient_user=self.patient,
            primary_professional=self.primary_professional,
            creating_professional=self.primary_professional,
        )

        # Set up session
        self.client.login(
            username=self.primary_pro_username, password=self.primary_pro_password
        )
        session = self.client.session
        session["domain_id"] = str(self.domain.id)
        session.save()

    def test_invite_existing_provider_by_id(self):
        url = reverse("appeals-invite-provider")

        response = self.client.post(
            url,
            json.dumps(
                {
                    "professional_id": self.secondary_professional.id,
                    "appeal_id": self.appeal.id,
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("message", response.json())
        self.assertEqual(response.json()["message"], "Provider invited successfully")

        # Verify the relation was created
        relation = SecondaryAppealProfessionalRelation.objects.filter(
            appeal=self.appeal, professional=self.secondary_professional
        ).exists()
        self.assertTrue(relation)

    def test_invite_existing_provider_by_email(self):
        url = reverse("appeals-invite-provider")

        response = self.client.post(
            url,
            json.dumps({"email": "secondary@example.com", "appeal_id": self.appeal.id}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("message", response.json())
        self.assertEqual(response.json()["message"], "Provider invited successfully")

        # Verify the relation was created
        relation = SecondaryAppealProfessionalRelation.objects.filter(
            appeal=self.appeal, professional=self.secondary_professional
        ).exists()
        self.assertTrue(relation)

    def test_invite_new_provider_by_email(self):
        url = reverse("appeals-invite-provider")
        new_provider_email = "new_provider@example.com"

        response = self.client.post(
            url,
            json.dumps({"email": new_provider_email, "appeal_id": self.appeal.id}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("message", response.json())
        self.assertEqual(response.json()["message"], "Provider invited successfully")

        # No relation should be created since the provider doesn't exist yet
        relation = SecondaryAppealProfessionalRelation.objects.filter(
            appeal=self.appeal
        ).exists()
        self.assertFalse(relation)


class StatisticsTest(APITestCase):
    """Test the statistics API endpoints."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        # Create domain
        self.domain = UserDomain.objects.create(
            name="testdomain",
            visible_phone_number="1234567890",
            internal_phone_number="0987654321",
            active=True,
            display_name="Test Domain",
            business_name="Test Business",
            country="USA",
            state="CA",
            city="Test City",
            address1="123 Test St",
            zipcode="12345",
        )

        # Create professional user
        self.pro_user = User.objects.create_user(
            username=f"prouser🐼{self.domain.id}",
            password="testpass",
            email="pro@example.com",
        )
        self.pro_username = f"prouser🐼{self.domain.id}"
        self.pro_password = "testpass"
        self.professional = ProfessionalUser.objects.create(
            user=self.pro_user, active=True, npi_number="1234567890"
        )
        self.pro_user.is_active = True
        self.pro_user.save()

        # Create ExtraUserProperties for professional
        ExtraUserProperties.objects.create(user=self.pro_user, email_verified=True)

        # Create professional domain relation
        ProfessionalDomainRelation.objects.create(
            professional=self.professional,
            domain=self.domain,
            active_domain_relation=True,
            admin=True,
            pending_domain_relation=False,
        )

        # Create patient users
        self.patient_user1 = User.objects.create_user(
            username="patientuser1",
            password="patientpass",
            email="patient1@example.com",
            first_name="Test1",
            last_name="Patient",
        )
        self.patient_user1.is_active = True
        self.patient_user1.save()
        self.patient1 = PatientUser.objects.create(user=self.patient_user1, active=True)

        # Create ExtraUserProperties for patient1
        ExtraUserProperties.objects.create(user=self.patient_user1, email_verified=True)

        # Create patient domain relation
        PatientDomainRelation.objects.create(
            patient=self.patient1,
            domain=self.domain,
        )

        self.patient_user2 = User.objects.create_user(
            username="patientuser2",
            password="patientpass",
            email="patient2@example.com",
            first_name="Test2",
            last_name="Patient",
        )
        self.patient_user2.is_active = True
        self.patient_user2.save()
        self.patient2 = PatientUser.objects.create(user=self.patient_user2, active=True)

        # Create ExtraUserProperties for patient2
        ExtraUserProperties.objects.create(user=self.patient_user2, email_verified=True)

        # Create patient domain relation
        PatientDomainRelation.objects.create(
            patient=self.patient2,
            domain=self.domain,
        )

        # Set up session
        self.client.login(username=self.pro_username, password=self.pro_password)
        session = self.client.session
        session["domain_id"] = str(self.domain.id)
        session.save()

        # Get current date and previous month
        self.now = timezone.now()
        self.previous_month = self.now - relativedelta(months=2)

        # Create denials and appeals for current month
        self.current_denial1 = Denial.objects.create(
            denial_text="Current test denial 1",
            primary_professional=self.professional,
            creating_professional=self.professional,
            patient_user=self.patient1,
            domain=self.domain,
            hashed_email=Denial.get_hashed_email(self.patient_user1.email),
        )

        self.current_appeal1 = Appeal.objects.create(
            for_denial=self.current_denial1,
            pending=False,
            sent=True,
            patient_user=self.patient2,
            primary_professional=self.professional,
            creating_professional=self.professional,
            domain=self.domain,
            mod_date=self.now.date(),
            creation_date=self.now.date(),
            response_date=self.now,
        )

        self.current_denial2 = Denial.objects.create(
            denial_text="Current test denial 2",
            primary_professional=self.professional,
            creating_professional=self.professional,
            patient_user=self.patient1,
            domain=self.domain,
            hashed_email=Denial.get_hashed_email(self.patient_user1.email),
        )

        self.current_appeal2 = Appeal.objects.create(
            for_denial=self.current_denial2,
            pending=True,
            sent=False,
            patient_user=self.patient1,
            primary_professional=self.professional,
            creating_professional=self.professional,
            domain=self.domain,
            mod_date=self.now.date(),
            creation_date=self.now.date(),
        )
        # Needs to be set after creation to avoid auto_now_add
        self.current_appeal2.creation_date = self.now.date() - relativedelta(days=10)
        self.current_appeal2.save()

        prev_month_date = (self.previous_month + relativedelta(days=5)).date()
        print(f"Creating old appeals around {prev_month_date}")

        # Create denials and appeals for previous month
        self.prev_denial1 = Denial.objects.create(
            denial_text="Previous test denial 1",
            primary_professional=self.professional,
            creating_professional=self.professional,
            patient_user=self.patient2,
            domain=self.domain,
            hashed_email=Denial.get_hashed_email(self.patient_user2.email),
        )

        self.prev_appeal1 = Appeal.objects.create(
            for_denial=self.prev_denial1,
            pending=False,
            sent=True,
            patient_user=self.patient2,
            primary_professional=self.professional,
            creating_professional=self.professional,
            domain=self.domain,
            mod_date=prev_month_date,
            response_date=self.previous_month + relativedelta(days=10),
        )
        # Needs to be set after creation to avoid auto_now_add
        self.prev_appeal1.creation_date = prev_month_date
        self.prev_appeal1.save()

        self.prev_denial2 = Denial.objects.create(
            denial_text="Previous test denial 2",
            primary_professional=self.professional,
            creating_professional=self.professional,
            patient_user=self.patient2,
            domain=self.domain,
            hashed_email=Denial.get_hashed_email(self.patient_user2.email),
        )

        self.prev_appeal2 = Appeal.objects.create(
            for_denial=self.prev_denial2,
            pending=True,
            sent=False,
            patient_user=self.patient2,
            primary_professional=self.professional,
            creating_professional=self.professional,
            domain=self.domain,
            mod_date=prev_month_date,
        )
        # Needs to be set after creation to avoid auto_now_add
        self.prev_appeal2.creation_date = prev_month_date
        self.prev_appeal2.save()

    def test_relative_statistics_endpoint(self):
        """Test the relative statistics endpoint with default Month over Month (MoM) comparison."""
        url = reverse("appeals-stats")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()

        # Verify all required fields exist
        required_fields = [
            "current_total_appeals",
            "current_success_rate",
            "current_estimated_payment_value",
            "current_total_patients",
            "previous_total_appeals",
            "previous_success_rate",
            "previous_estimated_payment_value",
            "previous_total_patients",
            "period_start",
            "period_end",
        ]

        for field in required_fields:
            self.assertIn(field, data)

        # Verify correct counts
        self.assertEqual(data["current_total_appeals"], 2)
        self.assertEqual(data["previous_total_appeals"], 2)

        # Verify patient counts - should now be total patients in domain
        self.assertEqual(data["current_total_patients"], 2)
        self.assertEqual(data["previous_total_patients"], 1)

    def test_absolute_statistics_endpoint(self):
        """Test the absolute statistics endpoint."""
        url = reverse("appeals-absolute-stats")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()

        # Verify all required fields exist
        required_fields = [
            "total_appeals",
            "success_rate",
            "estimated_payment_value",
            "total_patients",
        ]

        for field in required_fields:
            self.assertIn(field, data)

        # Verify counts - should be all appeals (current + previous = 4)
        self.assertEqual(data["total_appeals"], 4)

        # Verify success rate (no visible responses)
        self.assertEqual(data["success_rate"], 0.0)

        # Verify estimated payment is None until we implement it.
        self.assertEqual(data["estimated_payment_value"], None)

        # Verify patient count (should be all patients in domain = 2)
        self.assertEqual(data["total_patients"], 2)

        # Mark an appeal as replied to that is visible to the user
        self.current_appeal2.response_date = self.now
        self.current_appeal2.success = True
        self.current_appeal2.save()
        response = self.client.get(url)
        data = response.json()

        # Verify success rate (no visible responses)
        self.assertEqual(int(data["success_rate"]), 33)


class GetFullDetailsTest(APITestCase):
    """Test the get_full_details action of AppealViewSet."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        # Create domain
        self.domain = UserDomain.objects.create(
            name="testdomain",
            visible_phone_number="1234567890",
            internal_phone_number="0987654321",
            active=True,
            display_name="Test Domain",
            business_name="Test Business",
            country="USA",
            state="CA",
            city="Test City",
            address1="123 Test St",
            zipcode="12345",
        )

        # Create professional user
        self.pro_user = User.objects.create_user(
            username=f"prouser🐼{self.domain.id}",
            password="testpass",
            email="pro@example.com",
            first_name="Test",
            last_name="Provider",
        )
        self.pro_username = f"prouser🐼{self.domain.id}"
        self.pro_password = "testpass"
        self.professional = ProfessionalUser.objects.create(
            user=self.pro_user, active=True, npi_number="1234567890"
        )
        self.pro_user.is_active = True
        self.pro_user.save()
        ExtraUserProperties.objects.create(user=self.pro_user, email_verified=True)

        # Create professional domain relation
        ProfessionalDomainRelation.objects.create(
            professional=self.professional,
            domain=self.domain,
            active_domain_relation=True,
            admin=True,
            pending_domain_relation=False,
        )

        # Create patient user
        self.patient_user = User.objects.create_user(
            username="patientuser",
            password="patientpass",
            email="patient@example.com",
            first_name="Test",
            last_name="Patient",
        )
        self.patient_user.is_active = True
        self.patient_user.save()
        self.patient = PatientUser.objects.create(user=self.patient_user, active=True)

        # Create a denial
        self.denial = Denial.objects.create(
            denial_text="Test denial text",
            primary_professional=self.professional,
            creating_professional=self.professional,
            patient_user=self.patient,
            hashed_email=Denial.get_hashed_email(self.patient_user.email),
            insurance_company="Test Insurance Co",
            procedure="Test Procedure",
            diagnosis="Test Diagnosis",
            domain=self.domain,
        )

        # Create an appeal
        self.appeal = Appeal.objects.create(
            for_denial=self.denial,
            pending=True,
            patient_user=self.patient,
            primary_professional=self.professional,
            creating_professional=self.professional,
            domain=self.domain,
            appeal_text="This is a test appeal letter",
        )

        # Set up session
        self.client.login(username=self.pro_username, password=self.pro_password)
        session = self.client.session
        session["domain_id"] = str(self.domain.id)
        session.save()

    def test_get_full_details(self):
        """Test retrieving full details of an appeal."""
        url = reverse("appeals-get-full-details")
        response = self.client.get(f"{url}?pk={self.appeal.id}")

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify appeal data
        data = response.json()
        self.assertEqual(data["id"], self.appeal.id)
        self.assertEqual(data["appeal_text"], "This is a test appeal letter")
        self.assertEqual(data["pending"], True)

        # Verify denial data is included
        self.assertIsNotNone(data["denial"])
        self.assertEqual(data["denial"]["denial_text"], "Test denial text")
        self.assertEqual(data["denial"]["insurance_company"], "Test Insurance Co")
        self.assertEqual(data["denial"]["procedure"], "Test Procedure")
        self.assertEqual(data["denial"]["diagnosis"], "Test Diagnosis")

        # Verify domain data is included
        self.assertIsNotNone(data["in_userdomain"])
        self.assertEqual(data["in_userdomain"]["name"], "testdomain")
        self.assertEqual(data["in_userdomain"]["display_name"], "Test Domain")

        # Verify professional data is included
        self.assertIsNotNone(data["primary_professional"])
        self.assertTrue("id" in data["primary_professional"])
        self.assertEqual(data["primary_professional"]["npi_number"], "1234567890")
        self.assertEqual(data["primary_professional"]["fullname"], "Test Provider")

        # Verify patient data is included
        self.assertIsNotNone(data["patient"])
        self.assertTrue("id" in data["patient"])

    def test_get_full_details_unauthorized_user(self):
        """Test retrieving full details with an unauthorized user."""
        # Create another professional and patient not associated with this appeal
        other_pro_user = User.objects.create_user(
            username=f"otherprouser🐼{self.domain.id}",
            password="testpass",
            email="otherpro@example.com",
        )
        other_pro_user.is_active = True
        other_pro_user.save()
        other_professional = ProfessionalUser.objects.create(
            user=other_pro_user, active=True
        )

        # Create a different domain
        other_domain = UserDomain.objects.create(
            name="otherdomain",
            visible_phone_number="9876543210",
            active=True,
        )

        # Associate the other professional with the other domain
        ProfessionalDomainRelation.objects.create(
            professional=other_professional,
            domain=other_domain,
            active_domain_relation=True,
            admin=False,
            pending_domain_relation=False,
        )

        # Login as the other professional
        self.client.logout()
        self.client.login(
            username=f"otherprouser🐼{self.domain.id}", password="testpass"
        )
        session = self.client.session
        session["domain_id"] = str(other_domain.id)
        session.save()

        # Attempt to access the appeal
        url = reverse("appeals-get-full-details")
        response = self.client.get(f"{url}?pk={self.appeal.id}")

        # Should return 404 because this user doesn't have access to this appeal
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


from typing import Any, Dict


class DenialCreateWithExistingId(APITestCase):
    """Test creating a denial with an existing denial id."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        self.domain = UserDomain.objects.create(
            name="testdomain",
            visible_phone_number="1234567890",
            internal_phone_number="0987654321",
            active=True,
            display_name="Test Domain",
            business_name="Test Business",
            country="USA",
            state="CA",
            city="Test City",
            address1="123 Test St",
            zipcode="12345",
        )
        self.user = User.objects.create_user(
            username=f"testuser{self.domain.id}",
            password="testpass",
            email="test@example.com",
        )
        self.username = f"testuser{self.domain.id}"
        self.password = "testpass"
        self.prouser = ProfessionalUser.objects.create(
            user=self.user, active=True, npi_number="1234567890"
        )
        self.user.is_active = True
        self.user.save()
        ExtraUserProperties.objects.create(user=self.user, email_verified=True)

    def test_create_with_existing_denial_id(self):
        login_result = self.client.login(username=self.username, password=self.password)
        self.assertTrue(login_result)
        # Create a denial
        url = reverse("denials-list")
        email = "timbit@fighthealthinsurance.com"
        hashed_email = hashlib.sha512(email.encode("utf-8")).hexdigest()
        denial_text = "Test denial text"
        response = self.client.post(
            url,
            json.dumps(
                {
                    "email": email,
                    "denial_text": denial_text,
                    "pii": "true",
                    "tos": "true",
                    "privacy": "true",
                    "store_raw_email": "true",
                }
            ),
            content_type="application/json",
        )
        self.assertTrue(status.is_success(response.status_code))
        parsed: Dict[str, Any] = response.json()
        denial_id = parsed["denial_id"]
        # Create another denial with the same denial id
        response = self.client.post(
            url,
            json.dumps(
                {
                    "email": email,
                    "denial_text": denial_text,
                    "pii": "true",
                    "tos": "true",
                    "privacy": "true",
                    "store_raw_email": "true",
                    "denial_id": denial_id,
                }
            ),
            content_type="application/json",
        )
        self.assertTrue(status.is_success(response.status_code))
        parsed = response.json()
        self.assertEqual(parsed["denial_id"], denial_id)


class DuplicateUserDomainTest(APITestCase):
    """Test that a duplicate UserDomain request returns a non-200 response."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        # Create initial test domain
        self.domain_name = "testdomain"
        self.domain = UserDomain.objects.create(
            name=self.domain_name,
            visible_phone_number="1234567890",
            internal_phone_number="0987654321",
            active=True,
            display_name="Test Domain",
            business_name="Test Business",
            country="USA",
            state="CA",
            city="Test City",
            address1="123 Test St",
            zipcode="12345",
        )

    def test_duplicate_domain_creation(self):
        """Test that creating a domain with an existing name returns a non-200 response."""
        url = reverse("professional_user-list")

        # Data for creating a new professional with the same domain name
        data = {
            "user_signup_info": {
                "username": "newprouser",
                "password": "newLongerPasswordMagicCheetoCheeto123",
                "email": "newpro@example.com",
                "first_name": "New",
                "last_name": "User",
                "domain_name": self.domain_name,  # Same domain name as existing
                "visible_phone_number": "9876543210",  # Different phone number
                "continue_url": "http://example.com/continue",
            },
            "make_new_domain": True,
            "user_domain": {
                "name": self.domain_name,  # Same domain name as existing
                "visible_phone_number": "9876543210",
                "internal_phone_number": "0123456789",
                "display_name": "Duplicate Domain Test",
                "country": "USA",
                "state": "NY",
                "city": "New City",
                "address1": "456 Other St",
                "zipcode": "54321",
            },
        }

        response = self.client.post(
            url,
            json.dumps(data),
            content_type="application/json",
        )

        # Verify response is not a 200 OK
        self.assertNotEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotEqual(response.status_code, status.HTTP_201_CREATED)

        # Check error message in response
        response_data = response.json()
        self.assertIn("Domain", response_data["error"])

        # Verify no new domain was created with the same name
        domains_with_same_name = UserDomain.objects.filter(
            name=self.domain_name
        ).count()
        self.assertEqual(domains_with_same_name, 1)
