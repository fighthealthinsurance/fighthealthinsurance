"""The daily staff summary of shared plan documents.

Once a day the hourly plan documents sweep asks for it
(activities/plan_documents_sweep.send_plan_documents_staff_summary), and the
first ask of the UTC day sends it; later asks that day find it sent. It goes
to the staff list the professional signup notifications use
(utils.notify_staff, settings.PROFESSIONAL_SIGNUP_NOTIFICATION_EMAILS).

It covers yesterday and the 7 days up to and including yesterday, UTC: files
received, still waiting to be read (a stuck reading: anything from yesterday
or before has outlived many hourly sweeps), read as a document, received but
not readable, read but naming no company, read with only the company the
person typed (not confirmed), and opted-in copies left out (dropped). Counts
only: never a file
name, its text, a company or an address.

With nothing received or dropped in those 7 days, it sends one line on
Mondays ("no uploads") and nothing on other days, so a quiet week still
shows the summary is running.
"""

import datetime
from typing import Optional

from django.db.models import Sum
from django.utils import timezone

from loguru import logger

from fighthealthinsurance.models import PolicyDocument, SharedPlanDocumentsDay
from fighthealthinsurance.shared_plan_documents import (
    is_document,
    SOURCE_PERSON,
    give_back,
    is_unreadable,
    reserve,
)

#: The fields each window reports, in the order the email shows them.
FIELDS = (
    ("received", "Files received"),
    ("waiting", "Still waiting to be read (should be 0: reading is stuck)"),
    ("read", "Read as a document"),
    ("unreadable", "Received but not readable"),
    ("no_company", "Read, but naming no company"),
    ("company_typed", "Read, company typed by the person, not confirmed"),
    ("dropped", "Opted-in copies left out (refused, over budget or not saved)"),
)

SUBJECT = "Shared plan documents: daily summary"
QUIET_SUBJECT = "Shared plan documents: no uploads this week"


def _empty() -> dict[str, int]:
    return {key: 0 for key, _ in FIELDS}


def window_counts(first: datetime.date, last: datetime.date) -> dict[str, int]:
    """The counts for the UTC days ``first`` through ``last``. Files are
    counted by the day they were received, by where their reading is now;
    removed files and potential spam are left out."""
    counts = _empty()
    start = datetime.datetime.combine(first, datetime.time.min, datetime.timezone.utc)
    end = datetime.datetime.combine(
        last + datetime.timedelta(days=1), datetime.time.min, datetime.timezone.utc
    )
    rows = (
        PolicyDocument.objects.filter(
            source__in=PolicyDocument.SHARED_FILE_SOURCES,
            created_at__gte=start,
            created_at__lt=end,
        )
        .exclude(
            processing_status__in=(
                PolicyDocument.PROCESSING_REMOVED,
                PolicyDocument.PROCESSING_SPAM,
            )
        )
        .values_list(
            "processing_status",
            "cleaned_text",
            "plan_sponsor_id",
            "plan_sponsor_source",
        )
    )
    for status, text, sponsor_id, sponsor_source in rows:
        counts["received"] += 1
        if status == PolicyDocument.PROCESSING_RECEIVED:
            # From yesterday or before, so many hourly sweeps have passed it.
            counts["waiting"] += 1
        elif is_document(status, text):
            counts["read"] += 1
            if sponsor_id is None:
                counts["no_company"] += 1
            elif sponsor_source == SOURCE_PERSON:
                counts["company_typed"] += 1
        elif is_unreadable(status, text):
            counts["unreadable"] += 1
    dropped = SharedPlanDocumentsDay.objects.filter(
        day__gte=first, day__lte=last, name__in=SharedPlanDocumentsDay.DROPPED
    ).aggregate(total=Sum("count"))["total"]
    counts["dropped"] = int(dropped or 0)
    return counts


def summary_counts(today: datetime.date) -> dict[str, dict[str, int]]:
    """Yesterday's counts and the 7 days' up to and including yesterday."""
    yesterday = today - datetime.timedelta(days=1)
    return {
        "day": window_counts(yesterday, yesterday),
        "week": window_counts(today - datetime.timedelta(days=7), yesterday),
    }


def summary_text(today: datetime.date, counts: dict[str, dict[str, int]]) -> str:
    """The email's body: one line per count, yesterday then the 7 days."""
    yesterday = today - datetime.timedelta(days=1)
    lines = [f"Shared plan documents, counts only (UTC days, up to {yesterday}).", ""]
    width = max(len(label) for _, label in FIELDS)
    lines.append(f"{'':{width}}  Yesterday  Last 7 days")
    for key, label in FIELDS:
        lines.append(
            f"{label:{width}}  {counts['day'][key]:>9}  {counts['week'][key]:>11}"
        )
    lines += [
        "",
        "Files are counted on the day they arrived, by where their reading is now.",
        "The plan documents dashboard has the rest.",
    ]
    return "\n".join(lines)


def _quiet(counts: dict[str, dict[str, int]]) -> bool:
    week = counts["week"]
    return week["received"] == 0 and week["dropped"] == 0


def message_for(
    today: datetime.date, counts: dict[str, dict[str, int]]
) -> Optional[tuple[str, str]]:
    """The (subject, body) to send ``today``, or None for nothing: a quiet
    week sends one line on Mondays only."""
    if _quiet(counts):
        if today.weekday() != 0:
            return None
        return (
            QUIET_SUBJECT,
            "No shared plan documents were received or dropped in the last 7 days.",
        )
    return SUBJECT, summary_text(today, counts)


#: How many times one UTC day tries to send the summary: a failed send or
#: count gives the day back, and a later hourly sweep tries again, up to this.
MAX_TRIES_PER_DAY = 3


def send_summary_if_due(today: Optional[datetime.date] = None) -> bool:
    """Send today's summary unless it has gone already. True when an email
    was sent. The day is claimed while sending, so two sweeps at once send
    one. A send that fails, or a count that raises, gives the claim back so a
    later sweep that day tries again, at most MAX_TRIES_PER_DAY times."""
    from fighthealthinsurance.utils import notify_staff

    today = today or timezone.now().date()
    if _taken(SharedPlanDocumentsDay.STAFF_SUMMARY):
        return False
    claimed = reserve(SharedPlanDocumentsDay.STAFF_SUMMARY, 1, 1)
    if claimed is None:
        return False
    # A try is counted only by the sweep holding the claim.
    if (
        reserve(SharedPlanDocumentsDay.STAFF_SUMMARY_TRIES, 1, MAX_TRIES_PER_DAY)
        is None
    ):
        give_back(SharedPlanDocumentsDay.STAFF_SUMMARY, 1, claimed)
        return False
    try:
        message = message_for(today, summary_counts(today))
    except Exception as e:
        give_back(SharedPlanDocumentsDay.STAFF_SUMMARY, 1, claimed)
        logger.error(
            f"The shared plan documents staff summary failed: {type(e).__name__}"
        )
        return False
    if message is None:
        # Nothing to say today; the claim stays, so the day is done.
        return False
    subject, body = message
    if notify_staff(subject, body):
        return True
    give_back(SharedPlanDocumentsDay.STAFF_SUMMARY, 1, claimed)
    logger.error("The shared plan documents staff summary was not sent")
    return False


def _taken(name: str) -> bool:
    return SharedPlanDocumentsDay.objects.filter(
        day=timezone.now().date(), name=name, count__gt=0
    ).exists()
