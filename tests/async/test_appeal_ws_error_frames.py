"""Transport-edge behavior of the appeal-generation WebSocket.

1. A server-side generation failure emits an in-band ``type: error`` frame
   before the socket closes, so the client can tell "generation failed,
   retry" apart from "generation finished with nothing".
2. Bad magic-key credentials get an error frame BEFORE any generation work
   starts (parity with the REST fallback's 404 gate), and the generator is
   never constructed.
"""

import json
from unittest.mock import MagicMock, patch

from asgiref.sync import sync_to_async
from channels.testing import WebsocketCommunicator
from rest_framework.test import APITestCase

from fighthealthinsurance.models import Denial
from fighthealthinsurance.websockets import StreamingAppealsBackend

TEST_EMAIL = "wserrframes@example.com"
TEST_SEKRET = "sekret-ws-err-frames"


async def _make_denial():
    return await sync_to_async(Denial.objects.create)(
        denial_text="Test denial text for error frame tests",
        hashed_email=Denial.get_hashed_email(TEST_EMAIL),
        semi_sekret=TEST_SEKRET,
    )


async def _failing_generator(data):
    raise RuntimeError("boom: ML backend exploded")
    yield  # pragma: no cover - makes this an async generator


async def _collect_json_frames(communicator, limit=10, timeout=20):
    """Receive frames until one parses to a dict with type==error (or limit)."""
    frames = []
    for _ in range(limit):
        try:
            raw = await communicator.receive_from(timeout=timeout)
        except Exception:
            break
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            frames.append(json.loads(stripped))
        except json.JSONDecodeError:
            continue
        if frames[-1].get("type") == "error":
            break
    return frames


class AppealWsGenerationFailureTest(APITestCase):
    async def test_generation_failure_sends_error_frame_before_close(self):
        denial = await _make_denial()
        with patch(
            "fighthealthinsurance.common_view_logic.AppealsBackendHelper.generate_appeals",
            side_effect=_failing_generator,
        ):
            communicator = WebsocketCommunicator(
                StreamingAppealsBackend.as_asgi(), "/testws/"
            )
            connected, _ = await communicator.connect()
            self.assertTrue(connected)
            try:
                await communicator.send_json_to(
                    {
                        "denial_id": denial.denial_id,
                        "email": TEST_EMAIL,
                        "semi_sekret": TEST_SEKRET,
                    }
                )
                frames = await _collect_json_frames(communicator)
            finally:
                # The consumer re-raises after sending the error frame; the
                # communicator surfaces that on disconnect, which is expected.
                try:
                    await communicator.disconnect()
                except Exception:
                    pass

        error_frames = [f for f in frames if f.get("type") == "error"]
        self.assertTrue(
            error_frames,
            f"expected a type=error frame before close, got frames: {frames}",
        )
        self.assertIn("retry", error_frames[0].get("message", "").lower())


class AppealWsAuthPrecheckTest(APITestCase):
    async def test_bad_credentials_get_error_frame_without_generation(self):
        denial = await _make_denial()
        gen_mock = MagicMock(side_effect=_failing_generator)
        with patch(
            "fighthealthinsurance.common_view_logic.AppealsBackendHelper.generate_appeals",
            gen_mock,
        ):
            communicator = WebsocketCommunicator(
                StreamingAppealsBackend.as_asgi(), "/testws/"
            )
            connected, _ = await communicator.connect()
            self.assertTrue(connected)
            try:
                await communicator.send_json_to(
                    {
                        "denial_id": denial.denial_id,
                        "email": TEST_EMAIL,
                        "semi_sekret": "wrong-sekret",
                    }
                )
                frames = await _collect_json_frames(communicator)
            finally:
                try:
                    await communicator.disconnect()
                except Exception:
                    pass

        error_frames = [f for f in frames if f.get("type") == "error"]
        self.assertTrue(
            error_frames,
            f"expected a type=error frame for bad credentials, got: {frames}",
        )
        self.assertEqual(error_frames[0].get("message"), "Not found")
        gen_mock.assert_not_called()

    async def test_missing_credentials_get_error_frame_without_generation(self):
        gen_mock = MagicMock(side_effect=_failing_generator)
        with patch(
            "fighthealthinsurance.common_view_logic.AppealsBackendHelper.generate_appeals",
            gen_mock,
        ):
            communicator = WebsocketCommunicator(
                StreamingAppealsBackend.as_asgi(), "/testws/"
            )
            connected, _ = await communicator.connect()
            self.assertTrue(connected)
            try:
                await communicator.send_json_to({"denial_id": 424242})
                frames = await _collect_json_frames(communicator)
            finally:
                try:
                    await communicator.disconnect()
                except Exception:
                    pass

        error_frames = [f for f in frames if f.get("type") == "error"]
        self.assertTrue(error_frames)
        gen_mock.assert_not_called()
