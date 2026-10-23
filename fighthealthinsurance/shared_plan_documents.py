"""Plan documents people share with us for research.

Each shared file is one encrypted PolicyDocument marked with where it came
from (``source``) and the date of the agreement it was shared under. This
module stores them, with a fingerprint of each file so repeats can be
counted, and holds the limits every way in shares: a signed timestamp that
each upload form carries, and a daily budget per visitor and for everyone.

People share here in three ways: the share page, and the opt-in box on
Explain Policy and on the appeal's plan documents step, which shares a copy
of what was uploaded there.
"""

import hashlib
import hmac
import time
import uuid
from typing import Any, Mapping, Optional, Sequence

from django.conf import settings
from django.core import signing
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone
from loguru import logger

from fighthealthinsurance import consent
from fighthealthinsurance.chat_forms import policy_document_extension
from fighthealthinsurance.models import PolicyDocument

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


def _today() -> str:
    return timezone.now().date().isoformat()


def _used_today(request: HttpRequest) -> dict[str, int]:
    used = request.session.get(_SESSION_KEY)
    if not isinstance(used, dict) or used.get("day") != _today():
        return {"files": 0, "bytes": 0}
    return {"files": int(used.get("files", 0)), "bytes": int(used.get("bytes", 0))}


def shared_today() -> int:
    """Files shared by everyone since midnight UTC, from every page."""
    midnight = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return PolicyDocument.objects.filter(
        source__in=PolicyDocument.SHARED_SOURCES, created_at__gte=midnight
    ).count()


def daily_limit_problem(
    request: HttpRequest, files: Sequence[UploadedFile]
) -> Optional[str]:
    """None when ``files`` fit today's budgets, else what to tell the person.

    The visitor's own budget is generous for a person with many documents;
    the budget for everyone holds whatever a script does with cookies.
    """
    used = _used_today(request)
    size = sum(f.size or 0 for f in files)
    if (
        used["files"] + len(files) > _session_files_per_day()
        or used["bytes"] + size > _session_bytes_per_day()
    ):
        return SESSION_LIMIT_MESSAGE
    if shared_today() + len(files) > _files_per_day():
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


# ---- Storing shared files ---------------------------------------------------

# Names the SECRET_KEY sub-key, so these digests match no other feature's.
_FINGERPRINT_LABEL = b"fhi.shared-plan-document-fingerprint.v1"


def fingerprint(file: UploadedFile) -> str:
    """A keyed SHA-256 of the file's contents, the same for the same file.

    Keyed, so someone holding a file can't check it against our table
    without our key. Reads in chunks and leaves the file rewound.
    """
    secret = settings.SECRET_KEY
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    key = hmac.new(secret, _FINGERPRINT_LABEL, hashlib.sha256).digest()
    digest = hmac.new(key, digestmod=hashlib.sha256)
    for chunk in file.chunks():
        digest.update(chunk)
    file.seek(0)
    return digest.hexdigest()


def save_shared_documents(
    files: Sequence[UploadedFile],
    *,
    source: str,
    details: Mapping[str, Any],
    hashed_email: Optional[str],
    document_type: str = "other",
) -> list[PolicyDocument]:
    """One encrypted PolicyDocument per file, all or none. Only stores: no
    model reads the files here.

    ``details`` holds what is known about the plan: plan_category,
    plan_year, state, insurer_name, employer_name, insurance_company_obj and
    insurance_plan_obj, each optional.
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
        "insurance_company_obj": details.get("insurance_company_obj"),
        "insurance_plan_obj": details.get("insurance_plan_obj"),
    }
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
    except Exception:
        # The rows rolled back; their encrypted files would not.
        for doc in saved:
            try:
                doc.document_enc.delete(save=False)
            except Exception:
                logger.opt(exception=True).warning(
                    f"Failed to delete an unsaved shared file for {doc.pk}"
                )
        raise
    logger.info(f"Saved {len(saved)} shared plan document(s) from {source}")
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
        if daily_limit_problem(request, passing):
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
    scrambled email, so a removal request takes both."""
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
    the denial's scrambled email, so a removal request takes both."""
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
