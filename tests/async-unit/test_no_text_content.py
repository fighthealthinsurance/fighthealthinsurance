"""A completion with no text is a provider condition, not a bug."""

from unittest.mock import patch

import aiohttp
import pytest

from fighthealthinsurance.ml import ml_models
from fighthealthinsurance.ml.ml_models import NoAnswerText, RemoteFullOpenLike

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


@pytest.mark.asyncio
async def test_a_null_text_part_is_no_text(monkeypatch, make_fake_model_post):
    """A text part whose text is null used to be coerced to the string
    "None" and returned as the model's answer."""
    null_part = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": None}],
                },
                "finish_reason": "stop",
            }
        ]
    }
    model = RemoteFullOpenLike("http://parts.example/v1", "tok", "p2")
    monkeypatch.setattr(
        aiohttp.ClientSession,
        "post",
        make_fake_model_post(200, "{}", json_data=null_part),
    )
    assert await model._infer(system_prompts=["sys"], prompt="hi") is None


def _content_only(content):
    """A 200 whose only choice carries ``content`` and ran out of budget."""
    return {
        "choices": [
            {
                "message": {"role": "assistant", "content": content},
                "finish_reason": "length",
            }
        ]
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["", "\n\n"])
async def test_empty_content_raises_no_answer_text_when_asked(
    monkeypatch, make_fake_model_post, content
):
    """A reasoning model that spent its budget thinking can answer "" (or a
    blank line) rather than null. That used to come back as an answer, so
    entity extraction read the field as not in the letter rather than as a
    failed read."""
    model = RemoteFullOpenLike("http://reasoner.example/v1", "tok", "r3")
    monkeypatch.setattr(
        aiohttp.ClientSession,
        "post",
        make_fake_model_post(200, "{}", json_data=_content_only(content)),
    )
    with pytest.raises(NoAnswerText):
        await model._infer_no_context(
            system_prompts=["sys"], prompt="hi", raise_on_unavailable=True
        )


@pytest.mark.asyncio
async def test_empty_content_is_counted_as_no_text(monkeypatch, make_fake_model_post):
    model = RemoteFullOpenLike("http://reasoner.example/v1", "tok", "r4")
    monkeypatch.setattr(
        aiohttp.ClientSession,
        "post",
        make_fake_model_post(200, "{}", json_data=_content_only("")),
    )
    with patch.object(ml_models, "record_ml_failure") as failure:
        await model._infer(system_prompts=["sys"], prompt="hi")
    assert [c.args[1] for c in failure.call_args_list] == ["no_text"]
