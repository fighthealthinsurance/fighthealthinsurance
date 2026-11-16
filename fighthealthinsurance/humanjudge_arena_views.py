"""The OpenAI-compatible endpoint a HumanJudge arena calls for our models'
answers (see humanjudge_arena). Two routes, OpenAI's shapes:

- ``GET  /ziggy/humanjudge/v1/models``: the Arena's appeal-drafting models,
  the ones to enter in the arena.
- ``POST /ziggy/humanjudge/v1/chat/completions``: ``model`` names one of
  them, the last user message is the case; the reply is that model's letter.

Both answer 404 unless HUMAN_JUDGE_OAI_CALLBACK is set, and 401 to a call
without that key (as ``Authorization: Bearer`` or ``api-key``, the two
headers HumanJudge sends). No streaming: HumanJudge reads one JSON reply.
"""

import json
import time
from typing import Any, Optional

from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

# humanjudge_arena is imported in each view, not here: it pulls in the ML
# router, which the URL conf should not load.

# Far more than a case can be (humanjudge_arena.CASE_CHAR_LIMIT): bodies
# beyond this are refused before they are parsed.
_MAX_BODY_BYTES = 128 * 1024
_MAX_PROMPT_CHARS = 20000


def _error(status: int, message: str, code: str, kind: str) -> JsonResponse:
    response = JsonResponse(
        {"error": {"message": message, "type": kind, "code": code}}, status=status
    )
    response["Cache-Control"] = "no-store"
    return response


def _presented_key(request: HttpRequest) -> str:
    auth = request.headers.get("Authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return request.headers.get("api-key", "").strip()


def _refusal(request: HttpRequest) -> Optional[JsonResponse]:
    from fighthealthinsurance import humanjudge_arena

    if not humanjudge_arena.endpoint_enabled():
        return _error(404, "Not found", "not_found", "invalid_request_error")
    if not humanjudge_arena.key_matches(_presented_key(request)):
        return _error(401, "Invalid API key", "invalid_api_key", "authentication_error")
    return None


def _prompt_of(messages: Any) -> Optional[str]:
    """The last user message's text, from a string or a list of text parts."""
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = "\n".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        if isinstance(content, str) and content.strip():
            return content
        return None
    return None


@csrf_exempt
@require_GET
async def models_view(request: HttpRequest) -> JsonResponse:
    from fighthealthinsurance import humanjudge_arena

    refused = _refusal(request)
    if refused is not None:
        return refused
    data = [
        {
            "id": name,
            "object": "model",
            "created": 0,
            "owned_by": "fighthealthinsurance",
        }
        for name in humanjudge_arena.arena_models()
    ]
    response = JsonResponse({"object": "list", "data": data})
    response["Cache-Control"] = "no-store"
    return response


@csrf_exempt
@require_POST
async def chat_completions_view(request: HttpRequest) -> JsonResponse:
    from fighthealthinsurance import humanjudge_arena

    refused = _refusal(request)
    if refused is not None:
        return refused
    if len(request.body) > _MAX_BODY_BYTES:
        return _error(413, "Request too large", "too_large", "invalid_request_error")
    try:
        body = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        return _error(400, "Body is not JSON", "invalid_json", "invalid_request_error")
    if not isinstance(body, dict):
        return _error(
            400, "Body must be an object", "invalid_json", "invalid_request_error"
        )
    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        return _error(
            400, "model is required", "missing_model", "invalid_request_error"
        )
    if body.get("stream"):
        return _error(
            400,
            "Streaming is not supported",
            "stream_unsupported",
            "invalid_request_error",
        )
    prompt = _prompt_of(body.get("messages"))
    if prompt is None:
        return _error(
            400, "A user message is required", "missing_prompt", "invalid_request_error"
        )
    if len(prompt) > _MAX_PROMPT_CHARS:
        return _error(
            400, "The message is too long", "too_large", "invalid_request_error"
        )
    try:
        answer = await humanjudge_arena.answer_for_request(prompt, model.strip())
    except humanjudge_arena.ArenaError as e:
        response = _error(
            e.status,
            str(e),
            e.code,
            "server_error" if e.status >= 500 else "invalid_request_error",
        )
        if isinstance(e, humanjudge_arena.AnswerPending):
            response["Retry-After"] = "60"
        return response
    response = JsonResponse(
        {
            "id": f"chatcmpl-fhi-{answer.pk}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": answer.model_name,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": answer.content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
    )
    response["Cache-Control"] = "no-store"
    return response
