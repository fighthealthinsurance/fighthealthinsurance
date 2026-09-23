"""A completion with no text is a provider condition, not a bug."""

import aiohttp
import pytest

from fighthealthinsurance.ml.ml_models import RemoteFullOpenLike

NO_TEXT = {
    "choices": [
        {
            "message": {
                "role": "assistant",
                "content": None,
                "reasoning_content": "still thinking when the budget ran out",
            },
            "finish_reason": "length",
        }
    ]
}
PARTS = {
    "choices": [
        {
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "OK"},
                    {"type": "image_url", "image_url": {"url": "x"}},
                ],
            },
            "finish_reason": "stop",
        }
    ]
}


@pytest.mark.asyncio
async def test_null_content_is_one_warning_not_a_traceback(
    monkeypatch, make_fake_model_post, log_capture
):
    """A reasoning model that spent its budget thinking, or a tool-calls-only
    reply, used to raise into the catch-all: two ERROR lines and a traceback
    per occurrence on the appeal path."""
    model = RemoteFullOpenLike("http://reasoner.example/v1", "tok", "r1")
    monkeypatch.setattr(
        aiohttp.ClientSession, "post", make_fake_model_post(200, "{}", json_data=NO_TEXT)
    )
    with log_capture() as cap:
        result = await model._infer(system_prompts=["sys"], prompt="hi")

    assert result is None
    assert cap.messages("ERROR") == []
    assert any("no text content" in m for m in cap.messages("WARNING"))


@pytest.mark.asyncio
async def test_content_parts_are_joined(monkeypatch, make_fake_model_post):
    model = RemoteFullOpenLike("http://parts.example/v1", "tok", "p1")
    monkeypatch.setattr(
        aiohttp.ClientSession, "post", make_fake_model_post(200, "{}", json_data=PARTS)
    )
    result = await model._infer(system_prompts=["sys"], prompt="hi")
    assert result is not None
    assert result[0] == "OK"
