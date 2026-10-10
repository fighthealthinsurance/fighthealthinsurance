"""The agreements a person ticks before an appeal, and the policy versions
they tick them against. One source for the wording the pages show and the
record that is kept of it (ConsentRecord in models.py)."""

from __future__ import annotations

import datetime
from typing import Any, Mapping, Optional

from loguru import logger

# The "Last updated" date each policy page shows; tests hold them equal.
TERMS_VERSION = datetime.date(2026, 10, 5)
PRIVACY_VERSION = datetime.date(2026, 10, 5)

# A record's channel is who brought the case in: the site itself, or an AI
# assistant through one of its links, named by assistant_client (the label
# the link carried). Its finish_in is where the letters are written: on the
# site, or in the chat. So a link opened with "Open my appeal form" or
# "Finish on this site instead" is channel "assistant", finish_in "site".
# Denial.channel is not this: it is the spend channel (ml/spend.py), and is
# "assistant" only for a case agreed to on the chat path's terms page.
CHANNEL_SITE = "site"
CHANNEL_ASSISTANT = "assistant"
FINISH_ON_SITE = "site"
FINISH_IN_CHAT = "chat"

# The intake page's four boxes, in page order, as the person reads them.
BOXES: dict[str, str] = {
    "pii": "I've taken my personal details out of the letter above.",
    "privacy": "I have read and understand the privacy policy.",
    "tos": (
        "I agree to the terms of service. I'll use this site only for my own "
        "insurance appeals, or for someone I'm helping who asked me to, not to "
        "diagnose or treat any condition."
    ),
    "personalonly": (
        "This is for my own appeal or for someone I'm helping who asked me "
        "to. (Doctors, therapists and offices: see our professional version.)"
    ),
}


def boxes_as_shown(ticked: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Each box with its wording and whether it was ticked."""
    return [
        {"name": name, "label": label, "ticked": bool(ticked.get(name))}
        for name, label in BOXES.items()
    ]


def record_consent(
    denial_id: Any,
    ticked: Mapping[str, Any],
    *,
    channel: str = CHANNEL_SITE,
    on_behalf: bool = False,
    finish_in: str = FINISH_ON_SITE,
    assistant_client: str = "",
) -> Optional[Any]:
    """Keep what was ticked for this denial. Best effort: the appeal goes on
    if the record can't be written, and the failure is logged."""
    from fighthealthinsurance.models import ConsentRecord, Denial

    try:
        return ConsentRecord.objects.create(
            denial=Denial.objects.get(denial_id=denial_id),
            terms_version=TERMS_VERSION,
            privacy_version=PRIVACY_VERSION,
            boxes=boxes_as_shown(ticked),
            channel=channel,
            on_behalf=on_behalf,
            finish_in=finish_in,
            assistant_client=assistant_client[:80],
        )
    except Exception:
        logger.opt(exception=True).warning("Could not record the agreements")
        return None


def assistant_that_brought(denial: Any) -> Optional[str]:
    """The label of the assistant that brought this case in, by its latest
    record ("" when the link carried no name), or None when the site did or
    nothing was recorded. Best effort, like record_consent."""
    try:
        latest = (
            denial.consent_records.order_by("-pk")
            .values_list("channel", "assistant_client")
            .first()
        )
    except Exception:
        logger.opt(exception=True).warning("Could not read the earlier agreements")
        return None
    if latest is None or latest[0] != CHANNEL_ASSISTANT:
        return None
    return str(latest[1] or "")


async def aassistant_that_brought(denial: Any) -> Optional[str]:
    """assistant_that_brought for async code (native async ORM)."""
    try:
        latest = (
            await denial.consent_records.order_by("-pk")
            .values_list("channel", "assistant_client")
            .afirst()
        )
    except Exception:
        logger.opt(exception=True).warning("Could not read the earlier agreements")
        return None
    if latest is None or latest[0] != CHANNEL_ASSISTANT:
        return None
    return str(latest[1] or "")
