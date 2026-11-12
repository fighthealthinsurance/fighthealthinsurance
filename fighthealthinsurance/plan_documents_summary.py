"""The daily staff summary of shared plan documents.

Once a day the hourly plan documents sweep asks for it
(activities/plan_documents_sweep.send_plan_documents_staff_summary), and the
first ask of the UTC day sends it; later asks that day find it sent. It goes
to the staff list the professional signup notifications use
(utils.notify_staff, settings.PROFESSIONAL_SIGNUP_NOTIFICATION_EMAILS).

It covers yesterday and the 7 days up to and including yesterday, UTC: files
received; still waiting for a first reading (a stuck reading: anything from
yesterday or before has outlived many hourly sweeps); waiting to be read
again after a failed reading; read as a document; received but not readable,
and why (unreadable_reason); a person's own papers; read but not a plan; read
but naming no company; read with only the company the person typed (not
confirmed); opted-in copies left out (dropped); and files refused because
the object store did not hold them. Then, as they stand now: copies waiting
to be made again (shared_copy_retries.copies_waiting) and the last nightly
file check (shared_file_check). Counts only: never a file name, its text, a
company or an address.

With nothing received, dropped or refused in those 7 days, no copies
waiting and no file the last check found missing or damaged, it sends one
line on Mondays ("no uploads") and nothing on other days, so a quiet week
still shows the summary is running.
"""

import datetime
from typing import Any, Optional

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

#: Why a file was not readable, each its own row after "not readable"; rows
#: read before reasons were kept have none.
REASON_FIELDS = tuple(
    (f"unreadable_{reason}", f"Not readable: {label}")
    for reason, label in PolicyDocument.UNREADABLE_REASON_CHOICES
) + (
    ("unreadable_no_reason", "Not readable: no reason recorded (read before reasons)"),
)

#: The fields each window reports, in the order the email shows them.
FIELDS = (
    (
        ("received", "Files received"),
        ("waiting", "Waiting for a first reading (should be 0: reading is stuck)"),
        ("waiting_again", "Waiting to be read again after a failed reading"),
        ("read", "Read as a document"),
        ("unreadable", "Received but not readable"),
    )
    + REASON_FIELDS
    + (
        ("personal", "A person's own papers (file kept, no text)"),
        ("not_a_plan", "Read, but not a plan"),
        ("no_company", "Read, but naming no company"),
        ("company_typed", "Read, company typed by the person, not confirmed"),
        (
            "dropped",
            "Opted-in copies left out (refused, over budget or not saved)",
        ),
        ("no_bucket_share_page", "Refused on the share page: storage lacked the file"),
        ("no_bucket_opt_in", "Opted-in copies refused: storage lacked the file"),
    )
)

#: What the last nightly file check found, in the order the email shows it.
FILE_CHECK_FIELDS = (
    (SharedPlanDocumentsDay.FILE_CHECK_WHOLE, "Whole"),
    (SharedPlanDocumentsDay.FILE_CHECK_NO_SIZE, "Whole, no size recorded"),
    (SharedPlanDocumentsDay.FILE_CHECK_CUT_SHORT, "Cut short"),
    (SharedPlanDocumentsDay.FILE_CHECK_WRONG_SIZE, "Wrong size"),
    (SharedPlanDocumentsDay.FILE_CHECK_MISSING, "Missing"),
    (SharedPlanDocumentsDay.FILE_CHECK_UNKNOWN, "Could not tell"),
    (SharedPlanDocumentsDay.FILE_CHECK_NOT_CHECKED, "Not checked (past the bound)"),
)
#: A file the check found in one of these is a problem worth an email.
FILE_CHECK_PROBLEMS = (
    SharedPlanDocumentsDay.FILE_CHECK_CUT_SHORT,
    SharedPlanDocumentsDay.FILE_CHECK_WRONG_SIZE,
    SharedPlanDocumentsDay.FILE_CHECK_MISSING,
)
#: The day counters summed into a window's field of the same name.
_DAY_FIELDS = {
    "no_bucket_share_page": SharedPlanDocumentsDay.NO_BUCKET_SHARE_PAGE,
    "no_bucket_opt_in": SharedPlanDocumentsDay.NO_BUCKET_OPT_IN,
}

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
            "unreadable_reason",
            "read_attempts",
        )
    )
    for status, text, sponsor_id, sponsor_source, reason, attempts in rows:
        counts["received"] += 1
        if status == PolicyDocument.PROCESSING_RECEIVED:
            # From yesterday or before, so many hourly sweeps have passed it,
            # unless an earlier reading failed and it waits to be read again.
            counts["waiting_again" if attempts else "waiting"] += 1
        elif status == PolicyDocument.PROCESSING_PERSONAL:
            counts["personal"] += 1
        elif status == PolicyDocument.PROCESSING_NOT_A_PLAN:
            counts["not_a_plan"] += 1
        elif is_document(status, text):
            counts["read"] += 1
            if sponsor_id is None:
                counts["no_company"] += 1
            elif sponsor_source == SOURCE_PERSON:
                counts["company_typed"] += 1
        elif is_unreadable(status, text):
            counts["unreadable"] += 1
            key = f"unreadable_{reason}"
            counts[key if reason and key in counts else "unreadable_no_reason"] += 1
    days = SharedPlanDocumentsDay.objects.filter(day__gte=first, day__lte=last)
    dropped = days.filter(name__in=SharedPlanDocumentsDay.DROPPED).aggregate(
        total=Sum("count")
    )["total"]
    counts["dropped"] = int(dropped or 0)
    by_name = dict(
        days.filter(name__in=_DAY_FIELDS.values())
        .values("name")
        .annotate(total=Sum("count"))
        .values_list("name", "total")
    )
    for key, name in _DAY_FIELDS.items():
        counts[key] = int(by_name.get(name) or 0)
    return counts


def last_file_check(today: datetime.date) -> Optional[dict[str, Any]]:
    """The counts of the latest nightly file check up to ``today``, with its
    day, or None if none has run (the check keeps a few days only)."""
    latest = (
        SharedPlanDocumentsDay.objects.filter(
            day__lte=today, name__in=SharedPlanDocumentsDay.FILE_CHECK_COUNTS
        )
        .order_by("-day")
        .values_list("day", flat=True)
        .first()
    )
    if latest is None:
        return None
    found = dict(
        SharedPlanDocumentsDay.objects.filter(
            day=latest, name__in=SharedPlanDocumentsDay.FILE_CHECK_COUNTS
        ).values_list("name", "count")
    )
    return {
        "day": latest,
        "counts": {name: found.get(name, 0) for name, _ in FILE_CHECK_FIELDS},
    }


def now_counts(today: datetime.date) -> dict[str, Any]:
    """What stands now, not by window: copies waiting to be made again, and
    the last nightly file check."""
    from fighthealthinsurance.shared_copy_retries import copies_waiting

    return {"copies_waiting": copies_waiting(), "file_check": last_file_check(today)}


def summary_counts(today: datetime.date) -> dict[str, Any]:
    """Yesterday's counts, the 7 days' up to and including yesterday, and
    what stands now (now_counts)."""
    yesterday = today - datetime.timedelta(days=1)
    return {
        "day": window_counts(yesterday, yesterday),
        "week": window_counts(today - datetime.timedelta(days=7), yesterday),
        "now": now_counts(today),
    }


def summary_text(today: datetime.date, counts: dict[str, Any]) -> str:
    """The email's body: one line per count, yesterday then the 7 days."""
    yesterday = today - datetime.timedelta(days=1)
    lines = [f"Shared plan documents, counts only (UTC days, up to {yesterday}).", ""]
    width = max(len(label) for _, label in FIELDS)
    lines.append(f"{'':{width}}  Yesterday  Last 7 days")
    for key, label in FIELDS:
        lines.append(
            f"{label:{width}}  {counts['day'][key]:>9}  {counts['week'][key]:>11}"
        )
    now = counts["now"]
    lines += [
        "",
        f"Copies waiting to be made again now: {now['copies_waiting']}",
    ]
    check = now["file_check"]
    if check is None:
        lines.append("Nightly file check: none on record")
    else:
        lines.append(f"Nightly file check of {check['day']}:")
        found = check["counts"]
        width = max(len(label) for _, label in FILE_CHECK_FIELDS)
        for name, label in FILE_CHECK_FIELDS:
            lines.append(f"  {label:{width}}  {found[name]:>6}")
    lines += [
        "",
        "Files are counted on the day they arrived, by where their reading is now.",
        "The plan documents dashboard has the rest.",
    ]
    return "\n".join(lines)


def _quiet(counts: dict[str, Any]) -> bool:
    week = counts["week"]
    now = counts["now"]
    check = now["file_check"]
    return (
        week["received"] == 0
        and week["dropped"] == 0
        and week["no_bucket_share_page"] == 0
        and week["no_bucket_opt_in"] == 0
        and now["copies_waiting"] == 0
        and (
            check is None
            or not any(check["counts"][name] for name in FILE_CHECK_PROBLEMS)
        )
    )


def message_for(
    today: datetime.date, counts: dict[str, Any]
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
