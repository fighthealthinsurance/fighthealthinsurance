"""Plan documents people share with us for research.

Each shared file is one encrypted PolicyDocument marked with where it came
from (``source``) and the date of the agreement it was shared under. This
module stores them, with a fingerprint of each file so repeats can be
counted, and holds the limits every way in shares: a signed timestamp that
each upload form carries, a daily budget per visitor and for everyone, and a
daily count of share page submissions per address (take_share_place).
The share page has its own budget for everyone, and copies shared from other
pages have theirs, so neither uses the other's room. A budget for everyone is
taken in one conditional database update (reserve), so shares landing at the
same moment can't all fit in the same room.

People share here in three ways: the share page, and the opt-in box on
Explain Policy and on the appeal's plan documents step, which shares a copy
of what was uploaded there. The share page also takes links to public pages
with the plan's documents: one row each, with no file and the link stored
encrypted, that nothing reads or fetches and no budget counts. A removal request deletes a person's shared
files and links and keeps what they told us about the plan
(remove_shared_documents).
"""

import datetime
import hashlib
import hmac
import time
import uuid
from collections import Counter
from typing import Any, Mapping, NamedTuple, Optional, Sequence
from urllib.parse import urlsplit, urlunsplit

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.db.models import F, PositiveIntegerField
from django.db.models.functions import Greatest
from django.http import HttpRequest
from django.utils import timezone
from loguru import logger

from fighthealthinsurance import consent, temporal_client
from fighthealthinsurance.chat_forms import policy_document_extension
from fighthealthinsurance.models import (
    PolicyDocument,
    PolicyDocumentAnalysis,
    SharedPlanDocumentsDay,
    delete_policy_document_file_on_commit,
)

# ---- The signed timestamp on each upload form --------------------------------

_RENDER_SALT = "fhi.share-plan-documents.v1"
#: A person needs longer than this to choose files; a script often doesn't.
RENDER_MIN_SECONDS = 3
#: A page left open longer than this is sent again.
RENDER_MAX_AGE_SECONDS = 2 * 24 * 60 * 60
TRY_AGAIN = "Please choose your files and press Share again."


def render_token(at: Optional[float] = None) -> str:
    """A signed note of when the form was drawn, for its hidden "rendered"."""
    return signing.dumps(int(time.time() if at is None else at), salt=_RENDER_SALT)


def render_token_ok(value: Optional[str]) -> bool:
    """True when ``value`` is a token we signed, at least RENDER_MIN_SECONDS
    and at most RENDER_MAX_AGE_SECONDS old."""
    try:
        rendered_at = signing.loads(
            value or "", salt=_RENDER_SALT, max_age=RENDER_MAX_AGE_SECONDS
        )
    except signing.BadSignature:
        return False
    if not isinstance(rendered_at, int):
        return False
    age = time.time() - rendered_at
    return RENDER_MIN_SECONDS <= age <= RENDER_MAX_AGE_SECONDS


# ---- How much may be shared in a day ----------------------------------------

_SESSION_KEY = "shared_plan_documents_today"
SESSION_LIMIT_MESSAGE = (
    "You've shared a lot today. Thank you! Please share the rest tomorrow."
)
DAILY_LIMIT_MESSAGE = "We've had a lot of documents today. Please try again tomorrow."


def _session_files_per_day() -> int:
    return int(getattr(settings, "SHARED_PLAN_DOCUMENTS_SESSION_DAILY_FILES", 200))


def _session_bytes_per_day() -> int:
    return int(getattr(settings, "SHARED_PLAN_DOCUMENTS_SESSION_DAILY_BYTES", 1024**3))


def _files_per_day() -> int:
    return int(getattr(settings, "SHARED_PLAN_DOCUMENTS_DAILY_FILES", 1000))


def _opt_in_files_per_day() -> int:
    return int(getattr(settings, "SHARED_PLAN_DOCUMENTS_OPT_IN_DAILY_FILES", 1000))


def _budget(source: str) -> tuple[str, int]:
    """The budget for everyone that shares from ``source`` count against,
    and its daily limit: the share page's, or the one for opt-in copies."""
    if source == PolicyDocument.SOURCE_SHARED:
        return SharedPlanDocumentsDay.FILES, _files_per_day()
    return SharedPlanDocumentsDay.OPT_IN_FILES, _opt_in_files_per_day()


def _today() -> str:
    return timezone.now().date().isoformat()


def _used_today(request: HttpRequest) -> dict[str, int]:
    used = request.session.get(_SESSION_KEY)
    if not isinstance(used, dict) or used.get("day") != _today():
        return {"files": 0, "bytes": 0}
    return {"files": int(used.get("files", 0)), "bytes": int(used.get("bytes", 0))}


def _taken_today(name: str) -> int:
    """How much of today's ``name`` budget for everyone has been taken."""
    used = (
        SharedPlanDocumentsDay.objects.filter(day=timezone.now().date(), name=name)
        .values_list("count", flat=True)
        .first()
    )
    return used or 0


def shared_today(source: str = PolicyDocument.SOURCE_SHARED) -> int:
    """Files counted since midnight UTC against the budget for everyone that
    ``source`` shares from: the share page's by default."""
    return _taken_today(_budget(source)[0])


def reserve(name: str, amount: int, limit: int) -> Optional[datetime.date]:
    """Take ``amount`` from today's ``name`` budget of ``limit``. Returns the
    day it was taken from, or None when it doesn't fit. The check and the
    count are one update, so every pod sees the same room."""
    day = timezone.now().date()
    if amount > limit:
        return None
    SharedPlanDocumentsDay.objects.get_or_create(day=day, name=name)
    taken = SharedPlanDocumentsDay.objects.filter(
        day=day, name=name, count__lte=limit - amount
    ).update(count=F("count") + amount)
    return day if taken else None


def give_back(name: str, amount: int, day: datetime.date) -> None:
    """Return what reserve took, when what it was for didn't happen."""
    SharedPlanDocumentsDay.objects.filter(day=day, name=name).update(
        count=Greatest(F("count") - amount, 0, output_field=PositiveIntegerField())
    )


class OverDailyBudget(Exception):
    """Today's budget for everyone has no room for these files."""


def daily_limit_problem(
    request: HttpRequest,
    files: Sequence[UploadedFile],
    source: str = PolicyDocument.SOURCE_SHARED,
) -> Optional[str]:
    """None when ``files`` fit today's budgets, else what to tell the person.

    The visitor's own budget is generous for a person with many documents.
    The budget for everyone that ``source`` counts against is shared by
    every visitor.
    """
    used = _used_today(request)
    size = sum(f.size or 0 for f in files)
    if (
        used["files"] + len(files) > _session_files_per_day()
        or used["bytes"] + size > _session_bytes_per_day()
    ):
        return SESSION_LIMIT_MESSAGE
    name, limit = _budget(source)
    if _taken_today(name) + len(files) > limit:
        return DAILY_LIMIT_MESSAGE
    return None


def note_shared(request: HttpRequest, files: Sequence[UploadedFile]) -> None:
    """Count ``files`` against this visitor's budget for today."""
    used = _used_today(request)
    request.session[_SESSION_KEY] = {
        "day": _today(),
        "files": used["files"] + len(files),
        "bytes": used["bytes"] + sum(f.size or 0 for f in files),
    }


# ---- How many shares one address may send in a day --------------------------

# Counted the way the assistant terms page counts agreements
# (assistant_ip_limit): Cloudflare's address, an IPv6 address by its /64,
# kept only as a keyed digest of the address and the day. Each address's
# count is a row of the share page's daily counts, named by that digest.
_PER_ADDRESS_LABEL = b"fhi-shared-plan-documents-per-ip-v1"
_BUDGET_NAMES = (
    SharedPlanDocumentsDay.FILES,
    SharedPlanDocumentsDay.OPT_IN_FILES,
    SharedPlanDocumentsDay.REMINDERS,
)


class SharePlace(NamedTuple):
    name: str
    day: datetime.date


def _shares_per_address() -> int:
    return int(getattr(settings, "SHARED_PLAN_DOCUMENTS_DAILY_SHARES_PER_ADDRESS", 10))


def take_share_place(request: HttpRequest) -> Optional[SharePlace]:
    """Count one share, of files, links or both, from this visitor's address
    today, or None when the address has had its shares for the day. Earlier
    days' address rows are deleted first, so none outlives its day."""
    from fighthealthinsurance import assistant_ip_limit, client_network

    today = timezone.now().date()
    SharedPlanDocumentsDay.objects.filter(day__lt=today).exclude(
        name__in=_BUDGET_NAMES
    ).delete()
    name = client_network.period_key(
        _PER_ADDRESS_LABEL, today.isoformat(), assistant_ip_limit.address_of(request)
    )
    day = reserve(name, 1, _shares_per_address())
    return SharePlace(name, day) if day else None


def give_back_share_place(place: SharePlace) -> None:
    """Return the place of a share that wasn't saved."""
    give_back(place.name, 1, place.day)


# ---- Storing shared files ---------------------------------------------------

# Name the SECRET_KEY sub-keys, so these digests match no other feature's.
_FINGERPRINT_LABEL = b"fhi.shared-plan-document-fingerprint.v1"
_LINK_FINGERPRINT_LABEL = b"fhi.shared-plan-document-link-fingerprint.v1"


def _keyed_digest(label: bytes) -> "hmac.HMAC":
    secret = settings.SECRET_KEY
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    key = hmac.new(secret, label, hashlib.sha256).digest()
    return hmac.new(key, digestmod=hashlib.sha256)


def fingerprint(file: UploadedFile) -> str:
    """A keyed SHA-256 of the file's contents, the same for the same file.

    Keyed, so someone holding a file can't check it against our table
    without our key. Reads in chunks and leaves the file rewound.
    """
    digest = _keyed_digest(_FINGERPRINT_LABEL)
    for chunk in file.chunks():
        digest.update(chunk)
    file.seek(0)
    return digest.hexdigest()


def link_fingerprint(link: str) -> str:
    """A keyed SHA-256 of a shared link, the same for the same page however
    it was typed: the scheme and site in lower case, with no #part and no
    slash at the end. The link itself is stored encrypted, which differs on
    every save, so repeats are found by this."""
    parts = urlsplit(link.strip())
    normal = urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path.rstrip("/"),
            parts.query,
            "",
        )
    )
    digest = _keyed_digest(_LINK_FINGERPRINT_LABEL)
    digest.update(normal.encode("utf-8"))
    return digest.hexdigest()


def save_shared_documents(
    files: Sequence[UploadedFile],
    *,
    source: str,
    details: Mapping[str, Any],
    hashed_email: Optional[str],
    document_type: str = "other",
    links: Sequence[str] = (),
) -> list[PolicyDocument]:
    """One encrypted PolicyDocument per file, each marked received, and one
    per link (checked by the form), marked a link and stored encrypted with
    a keyed hash of it (link_fingerprint), all in one batch, all or
    none. Only stores: no model reads the files here, and nothing ever
    fetches a link. Once the rows commit, the files' reading is started on
    Temporal when it is on (temporal_client.dispatch_shared_plan_documents),
    away from the request. Links count against no budget.

    ``details`` holds what is known about the plan: plan_category,
    plan_year, state, insurer_name, employer_name, premium_amount,
    premium_period, who_is_covered, insurance_company_obj and
    insurance_plan_obj, each optional. Raises OverDailyBudget, storing
    nothing, when today's budget for everyone from ``source`` has no room
    for the files.
    """
    shared = {
        "source": source,
        "document_type": document_type,
        "plan_category": details.get("plan_category") or "unknown",
        "hashed_email": hashed_email or None,
        "shared_consent_version": consent.SHARING_VERSION,
        "shared_at": timezone.now(),
        "share_batch": uuid.uuid4(),
        "plan_year": details.get("plan_year"),
        "state": details.get("state") or "",
        "insurer_name": (details.get("insurer_name") or "").strip()[:200],
        "employer_name": (details.get("employer_name") or "").strip()[:200],
        "premium_amount": details.get("premium_amount"),
        "premium_period": details.get("premium_period") or "",
        "who_is_covered": details.get("who_is_covered") or "",
        "insurance_company_obj": details.get("insurance_company_obj"),
        "insurance_plan_obj": details.get("insurance_plan_obj"),
        "processing_status": PolicyDocument.PROCESSING_RECEIVED,
    }
    budget, limit = _budget(source)
    day = reserve(budget, len(files), limit) if files else None
    if files and day is None:
        raise OverDailyBudget()
    saved: list[PolicyDocument] = []
    try:
        with transaction.atomic():
            for uploaded in files:
                doc = PolicyDocument(**shared)
                doc.fingerprint = fingerprint(uploaded)
                # A neutral name, since the person's own file name can carry theirs.
                ext = policy_document_extension(uploaded.name or "")
                uploaded.name = f"shared-{doc.id.hex}{ext}"
                doc.document_enc = uploaded
                doc.filename = uploaded.name
                saved.append(doc)
                doc.save()
            for link in links:
                doc = PolicyDocument(
                    **{
                        **shared,
                        "source": PolicyDocument.SOURCE_SHARED_LINK,
                        "processing_status": PolicyDocument.PROCESSING_LINK,
                    },
                    shared_link=link,
                    shared_link_fingerprint=link_fingerprint(link),
                )
                saved.append(doc)
                doc.save()
    except Exception:
        if day is not None:
            give_back(budget, len(files), day)
        # The rows rolled back; their encrypted files would not.
        for doc in saved:
            if not doc.document_enc:
                continue
            try:
                doc.document_enc.delete(save=False)
            except Exception:
                logger.opt(exception=True).warning(
                    f"Failed to delete an unsaved shared file for {doc.pk}"
                )
        raise
    logger.info(
        f"Saved {len(files)} shared plan document(s) and {len(links)} link(s) "
        f"from {source}"
    )
    if files:
        share_batch = str(shared["share_batch"])
        # Best effort and never raises; the hourly sweep starts a lost one.
        transaction.on_commit(
            lambda: temporal_client.dispatch_shared_plan_documents(share_batch)
        )
    return saved


def match_insurer(insurer_name: str) -> Any:
    """The catalog's InsuranceCompany for a typed name, or None. Uses the
    strict name matcher, which needs no model call."""
    from fighthealthinsurance.pa_requirements import (
        resolve_insurance_company_by_name,
    )

    if not insurer_name.strip():
        return None
    try:
        return resolve_insurance_company_by_name(insurer_name.strip())
    except Exception:
        logger.opt(exception=True).warning("Could not match a shared insurer name")
        return None


# ---- Copies shared by ticking the box on another page -----------------------


def share_opt_in_copies(
    request: HttpRequest,
    files: Sequence[UploadedFile],
    *,
    source: str,
    details: Mapping[str, Any],
    hashed_email: Optional[str],
    document_type: str = "other",
) -> list[PolicyDocument]:
    """Shared copies of files a person also ticked the box to share, made
    once the page's own work is saved, as rows of their own.

    Only files that pass validate_shared_document are shared, within today's
    budgets; the rest are left out without a word, since the page they came
    from has already done what it was for. Never raises, for the same reason.
    """
    from django.core.exceptions import ValidationError

    from fighthealthinsurance.chat_forms import validate_shared_document

    try:
        passing: list[UploadedFile] = []
        for uploaded in files:
            if not uploaded:
                continue
            try:
                validate_shared_document(uploaded)
            except ValidationError:
                continue
            passing.append(uploaded)
        if len(passing) < len(files):
            logger.info(
                f"Left {len(files) - len(passing)} opted-in file(s) from {source} "
                "unshared: they failed the closer look"
            )
        if not passing:
            return []
        if daily_limit_problem(request, passing, source=source):
            logger.info(f"Left opted-in files from {source} unshared: over budget")
            return []
        saved = save_shared_documents(
            passing,
            source=source,
            details=details,
            hashed_email=hashed_email,
            document_type=document_type,
        )
        note_shared(request, passing)
        return saved
    except OverDailyBudget:
        logger.info(f"Left opted-in files from {source} unshared: over budget")
        return []
    except Exception:
        logger.opt(exception=True).error(
            f"Failed to save opted-in shared copies from {source}"
        )
        return []


def share_explain_policy_copy(
    request: HttpRequest, uploaded: UploadedFile, policy_doc: PolicyDocument
) -> list[PolicyDocument]:
    """A shared copy of an Explain Policy upload whose box was ticked. The
    copy has no session key, so the chat never picks it up, and the same
    scrambled email, so a removal request finds both."""
    return share_opt_in_copies(
        request,
        [uploaded],
        source=PolicyDocument.SOURCE_SHARED_EXPLAIN,
        details={"plan_category": policy_doc.plan_category},
        hashed_email=policy_doc.hashed_email or None,
        document_type=policy_doc.document_type,
    )


def share_appeal_plan_documents(
    request: HttpRequest, data: Mapping[str, Any]
) -> list[PolicyDocument]:
    """Shared copies of the files added on the appeal's plan documents step
    whose box was ticked. ``data`` is that step's cleaned form. The copies
    don't point at the appeal, so deleting one leaves the other; they carry
    the denial's scrambled email, so a removal request finds both."""
    from fighthealthinsurance.models import Denial

    files = [uploaded for uploaded in data.get("plan_documents") or [] if uploaded]
    if not files:
        return []
    try:
        denial = Denial.objects.get(
            hashed_email=Denial.get_hashed_email(data["email"]),
            denial_id=data["denial_id"],
            semi_sekret=data["semi_sekret"],
        )
        details = details_from_denial(denial)
    except Exception:
        logger.opt(exception=True).warning(
            "Could not read the appeal for an opted-in share"
        )
        return []
    return share_opt_in_copies(
        request,
        files,
        source=PolicyDocument.SOURCE_SHARED_APPEAL,
        details=details,
        hashed_email=denial.hashed_email,
    )


# A plan source names the kind of plan only for these. "Employer -- Private"
# is left out: church plans are private and not under ERISA.
_PLAN_SOURCE_CATEGORIES = {
    "medicare advantage": "medicare_advantage",
    "medicare regular": "medicare_traditional",
    "medicaid": "medicaid_chip",
    "veterans affairs": "va",
    "state marketplace / affordable care act": "aca_marketplace",
    "employer -- state government": "employer_non_erisa",
    "employer -- federal government": "employer_non_erisa",
    "employer -- other government": "employer_non_erisa",
}


def details_from_denial(denial: Any) -> dict[str, Any]:
    """What an appeal already knows about the plan, for its shared copies:
    insurer, plan, employer, state and kind of plan. Nothing about the
    person or the claim."""
    from fighthealthinsurance.state_help import get_states_sorted_by_name

    names = {
        " ".join(name.split()).lower()
        for name in denial.plan_source.values_list("name", flat=True)
    }
    categories = {_PLAN_SOURCE_CATEGORIES.get(name, "unknown") for name in names}
    plan_category = categories.pop() if len(categories) == 1 else "unknown"

    state = ""
    given = (denial.your_state or "").strip().lower()
    for known in get_states_sorted_by_name():
        if given and given in (known.abbreviation.lower(), known.name.lower()):
            state = known.abbreviation if len(known.abbreviation) == 2 else ""
            break

    company = denial.insurance_company_obj
    insurer_name = (denial.insurance_company or "").strip()
    if not insurer_name and company is not None:
        insurer_name = company.name
    if company is None:
        company = match_insurer(insurer_name)
    return {
        "plan_category": plan_category,
        "state": state,
        "insurer_name": insurer_name,
        "employer_name": (denial.employer_name or "").strip(),
        "insurance_company_obj": company,
        "insurance_plan_obj": denial.insurance_plan_obj,
    }


# ---- What a removal request keeps -------------------------------------------

#: Facts about the plan, not the person, that a removal request keeps on a
#: shared row. Who it covers and how often are kept only with the premium.
PLAN_FACT_FIELDS = (
    "plan_category",
    "plan_year",
    "state",
    "insurer_name",
    "employer_name",
    "premium_amount",
    "premium_period",
    "who_is_covered",
    "insurance_company_obj",
    "insurance_plan_obj",
)
#: The record of the share itself, kept with the plan facts.
SHARE_RECORD_FIELDS = (
    "id",
    "document_type",
    "source",
    "shared_consent_version",
    "shared_at",
    "created_at",
    "share_batch",
    "processed_at",
)
#: What a removal request clears on a row it keeps: the file or the link,
#: everything read from it, and what linked it to the person.
REMOVED_VALUES: dict[str, Any] = {
    "document_enc": None,
    "shared_link": "",
    "shared_link_fingerprint": "",
    "filename": "",
    "hashed_email": None,
    "session_key": None,
    "fingerprint": "",
    "cleaned_text": "",
    "processing_status": PolicyDocument.PROCESSING_REMOVED,
}


def has_plan_facts(doc: PolicyDocument) -> bool:
    """True when the row holds a fact about the plan worth keeping."""
    return bool(
        doc.plan_category not in ("", "unknown")
        or doc.plan_year
        or doc.state
        or doc.insurer_name
        or doc.employer_name
        or doc.premium_amount is not None
        or doc.insurance_company_obj_id
        or doc.insurance_plan_obj_id
    )


def remove_shared_documents(hashed_email: str) -> None:
    """Remove-my-data for the plan documents shared with this scrambled email.

    Each file and link is deleted, with a file's cleaned text, its
    fingerprint and the scrambled email. A row that holds facts about the
    plan (PLAN_FACT_FIELDS) keeps them and is marked removed, so no reading
    writes to it again; a row with none is deleted whole. Rows shared with no email, or another
    email, are untouched.
    """
    with transaction.atomic():
        rows = list(
            PolicyDocument.objects.filter(
                hashed_email=hashed_email,
                source__in=PolicyDocument.SHARED_SOURCES,
            )
        )
        nothing_to_keep = [doc.pk for doc in rows if not has_plan_facts(doc)]
        # The row's own delete signal takes its file.
        PolicyDocument.objects.filter(pk__in=nothing_to_keep).delete()
        for doc in rows:
            if doc.pk in nothing_to_keep:
                continue
            values = dict(REMOVED_VALUES)
            if doc.premium_amount is None:
                # Without an amount these describe the household, not the plan.
                values.update(premium_period="", who_is_covered="")
            PolicyDocument.objects.filter(pk=doc.pk).update(**values)
            PolicyDocumentAnalysis.objects.filter(policy_document_id=doc.pk).delete()
            delete_policy_document_file_on_commit(doc.document_enc, doc.pk)
        if rows:
            logger.info(
                f"Removed {len(rows)} shared plan document(s): "
                f"{len(rows) - len(nothing_to_keep)} kept for their plan facts"
            )


# ---- Counting plans ---------------------------------------------------------

#: The share page's goal, shown through its day. The running count appears
#: only from GOAL_SHOWN_FROM plans, so nobody lands on "2 of 100".
GOAL_PLANS = 100
GOAL_DATE = datetime.date(2027, 1, 15)
GOAL_SHOWN_FROM = 20
#: The page's count is at most this old, and the same for every visitor.
PLAN_COUNT_SECONDS = 10 * 60
_PLAN_COUNT_KEY = "shared-plan-documents-distinct-plans"


class Share(NamedTuple):
    """One share (a batch of files and links), as the counts see it."""

    shared_at: datetime.datetime
    new_plan: bool
    files: int
    links: int
    plan_category: str
    plan_year: Optional[int]
    premium_answered: bool


def plan_shares() -> list[Share]:
    """Every share still holding a file or link, oldest first, marked with
    whether it is a new plan."""
    # Each share is one plan unless every file and link in it came in an
    # earlier share. Removed rows count for nothing.
    rows = (
        PolicyDocument.objects.filter(source__in=PolicyDocument.SHARED_SOURCES)
        .exclude(processing_status=PolicyDocument.PROCESSING_REMOVED)
        .order_by("shared_at", "created_at", "pk")
        .values_list(
            "pk",
            "share_batch",
            "shared_at",
            "created_at",
            "source",
            "fingerprint",
            "shared_link_fingerprint",
            "plan_category",
            "plan_year",
            "premium_amount",
        )
    )
    batches: dict[Any, dict[str, Any]] = {}
    for (
        pk,
        batch,
        shared_at,
        created_at,
        source,
        file_hash,
        link_hash,
        category,
        year,
        premium,
    ) in rows:
        found = batches.setdefault(
            batch or pk,
            {
                "at": shared_at or created_at,
                "files": set(),
                "links": set(),
                "unknown": False,
                "file_count": 0,
                "link_count": 0,
                "category": category,
                "year": year,
                "premium": premium is not None,
            },
        )
        is_link = source == PolicyDocument.SOURCE_SHARED_LINK
        found["link_count" if is_link else "file_count"] += 1
        digest = link_hash if is_link else file_hash
        if digest:
            found["links" if is_link else "files"].add(digest)
        else:
            found["unknown"] = True
    seen_files: set[str] = set()
    seen_links: set[str] = set()
    shares = []
    for found in batches.values():
        new_plan = bool(
            found["unknown"]
            or found["files"] - seen_files
            or found["links"] - seen_links
        )
        seen_files |= found["files"]
        seen_links |= found["links"]
        shares.append(
            Share(
                shared_at=found["at"],
                new_plan=new_plan,
                files=found["file_count"],
                links=found["link_count"],
                plan_category=found["category"] or "unknown",
                plan_year=found["year"],
                premium_answered=found["premium"],
            )
        )
    return shares


def distinct_plan_count() -> int:
    """How many different plans have been shared (plan_shares), counted at
    most once every PLAN_COUNT_SECONDS for all visitors alike."""
    count = cache.get(_PLAN_COUNT_KEY)
    if count is None:
        count = sum(1 for share in plan_shares() if share.new_plan)
        cache.set(_PLAN_COUNT_KEY, count, PLAN_COUNT_SECONDS)
    return int(count)


def goal_progress(today: datetime.date) -> Optional[dict[str, Any]]:
    """The share page's goal through its day, with the count once it reaches
    GOAL_SHOWN_FROM, or None after the day. A count that fails is left off."""
    if today > GOAL_DATE:
        return None
    try:
        count: Optional[int] = distinct_plan_count()
    except Exception:
        logger.opt(exception=True).warning("Could not count the shared plans")
        count = None
    if count is not None and count < GOAL_SHOWN_FROM:
        count = None
    return {
        "plans": GOAL_PLANS,
        "by": GOAL_DATE,
        "count": count,
        # How far along, for the bar: never past full.
        "percent": min(count or 0, GOAL_PLANS) * 100 // GOAL_PLANS,
    }


def share_counts(today: datetime.date, weeks: int = 12) -> dict[str, Any]:
    """The staff counts: shares and new plans each week (the last ``weeks``,
    newest first), and the plans by kind, by year and by whether the premium
    question was answered."""
    from fighthealthinsurance.client_network import week_start

    shares = plan_shares()
    plans = [share for share in shares if share.new_plan]
    by_week: dict[datetime.date, list[int]] = {}
    for share in shares:
        week = week_start(timezone.localdate(share.shared_at))
        counts = by_week.setdefault(week, [0, 0])
        counts[0] += 1
        counts[1] += int(share.new_plan)
    week_rows: list[dict[str, Any]] = []
    if shares:
        first = min(by_week)
        week = week_start(today)
        while week >= first and len(week_rows) < weeks:
            got = by_week.get(week, [0, 0])
            week_rows.append({"week": week, "shares": got[0], "plans": got[1]})
            week -= datetime.timedelta(days=7)
    labels = dict(PolicyDocument.PLAN_CATEGORY_CHOICES)
    kinds = Counter(share.plan_category for share in plans)
    years = Counter(share.plan_year for share in plans)
    this_year, next_year = today.year, today.year + 1
    return {
        "shares": len(shares),
        "plans": len(plans),
        "goal": GOAL_PLANS,
        "goal_date": GOAL_DATE,
        "weeks": week_rows,
        "kinds": [
            {"kind": labels.get(kind, kind), "plans": n}
            for kind, n in sorted(kinds.items(), key=lambda item: (-item[1], item[0]))
        ],
        "years": [
            {"year": str(next_year), "plans": years.get(next_year, 0)},
            {"year": str(this_year), "plans": years.get(this_year, 0)},
            {
                "year": "Other years",
                "plans": sum(
                    n
                    for year, n in years.items()
                    if year is not None and year not in (this_year, next_year)
                ),
            },
            {"year": "Not given", "plans": years.get(None, 0)},
        ],
        "premium_answered": sum(1 for share in plans if share.premium_answered),
    }
