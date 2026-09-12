"""The extraction socket resolves the case before it touches it.

The two other streaming consumers in ``websockets.py`` resolve the
(denial_id, email, semi_sekret) triple through
``common_view_logic.get_denial_for_action`` before they spend anything. This
one checked only that a ``denial_id`` was present in the message and then
called ``extract_entity``, which is not a read: it writes the row and spends
one of the three automatic-read attempts a case gets.

The page has always sent all three values, so the fix costs the client
nothing. These tests pin the gate, and pin that a request which does not
resolve leaves the row exactly as it was.
"""

import json

import pytest
from asgiref.sync import sync_to_async
from channels.testing import WebsocketCommunicator

from fighthealthinsurance import common_view_logic
from fighthealthinsurance.models import Denial
from fighthealthinsurance.websockets import StreamingEntityBackend

pytestmark = pytest.mark.django_db(transaction=True)

WATCHED = (
    "extract_attempts",
    "extract_procedure_diagnosis_finished",
    "procedure",
    "diagnosis",
    "candidate_procedure",
    "candidate_diagnosis",
)


async def _make_denial() -> Denial:
    return await sync_to_async(Denial.objects.create)(
        denial_text="A denial to extract from.",
        hashed_email=Denial.get_hashed_email("someone@example.com"),
        semi_sekret="the-real-secret",
    )


async def _fields(denial: Denial) -> dict:
    fresh = await sync_to_async(Denial.objects.get)(denial_id=denial.denial_id)
    return {name: getattr(fresh, name) for name in WATCHED}


async def _send(payload: dict) -> tuple[list, bool]:
    """Drive the consumer with one payload; return (frames, still_open)."""
    communicator = WebsocketCommunicator(StreamingEntityBackend.as_asgi(), "/ws/streaming-entity-backend/")
    connected, _ = await communicator.connect()
    assert connected
    frames = []
    try:
        await communicator.send_to(text_data=json.dumps(payload))
        while True:
            try:
                frames.append(await communicator.receive_from(timeout=3))
            except Exception:
                break
        still_open = not await communicator.receive_nothing(timeout=0.1)
    finally:
        await communicator.disconnect()
    return frames, still_open


@pytest.mark.asyncio
async def test_a_request_that_does_not_resolve_never_reaches_the_extractor(monkeypatch):
    denial = await _make_denial()
    before = await _fields(denial)

    def _must_not_run(*args, **kwargs):
        raise AssertionError("extract_entity ran for a request that did not resolve")

    monkeypatch.setattr(
        common_view_logic.DenialCreatorHelper, "extract_entity", _must_not_run
    )

    rejected = [
        {"denial_id": denial.denial_id},
        {"denial_id": denial.denial_id, "email": "someone@example.com"},
        {"denial_id": denial.denial_id, "email": "wrong@example.com", "semi_sekret": "the-real-secret"},
        {"denial_id": denial.denial_id, "email": "someone@example.com", "semi_sekret": "wrong"},
        {"denial_id": denial.denial_id + 10_000, "email": "someone@example.com", "semi_sekret": "the-real-secret"},
    ]
    replies = []
    for payload in rejected:
        frames, _ = await _send(payload)
        replies.append(frames)
        # The row is untouched, field by field, including the attempt counter.
        assert await _fields(denial) == before, f"the row changed for {payload!r}"

    # Every rejection looks the same, so the reply says nothing about which
    # part did not match or whether the case exists at all.
    assert all(r == replies[0] for r in replies), f"rejections differ: {replies}"
    assert replies[0], "a rejected request got no reply at all"
    assert "Not found" in replies[0][0]


@pytest.mark.asyncio
async def test_the_matching_triple_still_runs_the_extractor(monkeypatch):
    denial = await _make_denial()
    ran = {"count": 0}

    async def _fake_extract(denial_id):
        ran["count"] += 1
        assert denial_id == denial.denial_id
        yield "claim id"
        yield "Extraction complete"

    monkeypatch.setattr(
        common_view_logic.DenialCreatorHelper, "extract_entity", _fake_extract
    )
    frames, _ = await _send(
        {
            "denial_id": denial.denial_id,
            "email": "someone@example.com",
            "semi_sekret": "the-real-secret",
        }
    )
    assert ran["count"] == 1, "the extractor did not run for a request that resolves"
    assert any("Extraction complete" in f for f in frames), frames
