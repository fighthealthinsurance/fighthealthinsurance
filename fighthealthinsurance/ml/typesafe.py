"""Transport for TypeSafe's System One API, shared by every feature that asks it
typed questions about a piece of text (draft scoring in letter_quality.py, and
denial triage). One place for the URL, the auth header, the model, the timeout
and the status check, so a feature module only decides WHAT to ask.

The request follows the documented API (docs.typesafe.ai/api): a JSON body of
``state`` (the text to evaluate), ``model`` and ``questions``, with the key as
a Bearer token.

Never logs the state. A response body is never surfaced either: on a non-200
the error carries the status alone, because an error body could quote the
state back. And the request, which carries the API key and the state, only
ever goes over https: a URL with any other scheme is refused before a session
exists.
"""

import re
import typing
from urllib.parse import urlsplit

import aiohttp
from django.conf import settings

# The pinned Jev release, used when TYPESAFE_MODEL is unset or empty. A pinned
# id rather than the "jev-latest" alias, because TypeSafe can repoint an alias
# to a new release and that moves every score we record. Features record the
# model the response names, so a change here shows up in their data as a new
# series rather than a silently different number.
DEFAULT_MODEL = "jev-1.13.0"

# A model id or alias as TypeSafe spells them ("jev-1.13.0", "jev-latest").
# The same shape the features' provenance strings allow, so a model name can
# never break or forge one.
_MODEL_NAME = re.compile(r"[A-Za-z0-9._-]{1,48}")

# Measured in the evaluation run, and well inside the documented budget of
# 32k tokens for the state plus the longest question.
STATE_CHAR_CAP = 24_000


class TypeSafeError(Exception):
    """The API did not return a usable answer set.

    ``status`` is the HTTP status when the API answered at all, so a caller
    can tell a rejected key (401), exhausted credits (402), a request that
    failed validation (422), a rate limit (429) or an overload (529) from an
    outage (other 5xx) without parsing the message.
    """

    def __init__(self, message: str, status: typing.Optional[int] = None):
        super().__init__(message)
        self.status = status


def configured() -> bool:
    return bool(getattr(settings, "TYPESAFE_API_KEY", None))


def model_name() -> str:
    """The model every request names: TYPESAFE_MODEL, or the pinned release
    when the setting is unset or empty."""
    return str(getattr(settings, "TYPESAFE_MODEL", "") or "").strip() or DEFAULT_MODEL


def reported_model(payload: typing.Any) -> str:
    """The model a response says answered, for the provenance strings the
    features record. TypeSafe reports the versioned id (jev-1.13.0 even when
    the request named the jev-latest alias), so a repointed alias shows up as
    a new name. A response that names none, or names something that is not a
    plain model name, is recorded under the name the request sent."""
    answered = payload.get("model") if isinstance(payload, dict) else None
    name = str(answered).strip() if answered else ""
    return name if _MODEL_NAME.fullmatch(name) else model_name()


async def ask(
    state: str,
    questions: dict[str, dict[str, typing.Any]],
    *,
    timeout_seconds: float,
) -> typing.Any:
    """POST one state and a set of typed questions; return the raw JSON.

    Raises TypeSafeError on a non-200, and lets aiohttp/asyncio errors
    propagate: callers decide what a failure means for their feature.
    """
    url = str(getattr(settings, "TYPESAFE_API_URL", "") or "")
    if urlsplit(url).scheme.lower() != "https":
        # The bearer token and the state must never travel in the clear
        # (review). Refused here, before anything is built or sent.
        raise TypeSafeError("TYPESAFE_API_URL must use https")
    model = model_name()
    if not _MODEL_NAME.fullmatch(model):
        # Refused rather than sent: when a response names no model, this name
        # goes into the recorded provenance, so it must fit that format.
        raise TypeSafeError("TYPESAFE_MODEL is not a model name")
    body = {
        "state": state[:STATE_CHAR_CAP],
        "model": model,
        "questions": questions,
    }
    headers = {
        "Authorization": f"Bearer {settings.TYPESAFE_API_KEY}",
        "Content-Type": "application/json",
    }
    client_timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with aiohttp.ClientSession(timeout=client_timeout) as session:
        # No redirects: a 307 or 308 toward http would make aiohttp resend
        # the state in the clear (review). A 3xx is just a non-200 here.
        async with session.post(
            url, json=body, headers=headers, allow_redirects=False
        ) as response:
            if response.status != 200:
                raise TypeSafeError(f"HTTP {response.status}", status=response.status)
            return await response.json()
