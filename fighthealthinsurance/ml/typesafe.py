"""Transport for TypeSafe's System One API, shared by every feature that asks it
typed questions about a document (draft scoring in letter_quality.py, and
denial triage). One place for the URL, the auth header, the timeout and the
status check, so a feature module only decides WHAT to ask.

Never logs the document. A response body is never surfaced either: on a
non-200 the error carries the status alone, because an error body could quote
the document back.
"""

import typing

import aiohttp
from django.conf import settings

# Cheapest current model; features pin their own scorer name so a model change
# here is visible in the data they record.
DEFAULT_MODEL = "speed_latest"

# Measured in the evaluation run; System One rejects longer documents.
DOCUMENT_CHAR_CAP = 24_000


class TypeSafeError(Exception):
    """The API did not return a usable answer set."""


def configured() -> bool:
    return bool(getattr(settings, "TYPESAFE_API_KEY", None))


async def ask(
    document: str,
    questions: dict[str, dict[str, typing.Any]],
    *,
    timeout_seconds: float,
    model: str = DEFAULT_MODEL,
) -> typing.Any:
    """POST one document and a set of typed questions; return the raw JSON.

    Raises TypeSafeError on a non-200, and lets aiohttp/asyncio errors
    propagate: callers decide what a failure means for their feature.
    """
    body = {
        "document": document[:DOCUMENT_CHAR_CAP],
        "model": model,
        "questions": questions,
    }
    headers = {
        "Authorization": f"Bearer {settings.TYPESAFE_API_KEY}",
        "Content-Type": "application/json",
    }
    client_timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with aiohttp.ClientSession(timeout=client_timeout) as session:
        async with session.post(
            settings.TYPESAFE_API_URL, json=body, headers=headers
        ) as response:
            if response.status != 200:
                raise TypeSafeError(f"HTTP {response.status}")
            return await response.json()
