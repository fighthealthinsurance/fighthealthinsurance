"""Subscriber list hygiene: find junk/suspicious signups and clean them up.

Our signup paths are open forms (denial flow, chat leads, explain-denial, the
REST API), so the mailing list accumulates rows nobody should ever be mailed:
bot submissions with URLs stuffed into the name field, addresses at reserved
domains, case-variant duplicates of the same mailbox (``get_or_create(email=...)``
is case-sensitive), and internal test signups.

This module is the shared brain for that cleanup. It only *analyzes* -- callers
decide what to do:

  * ``python manage.py cleanup_subscribers`` -- report, and delete with ``--apply``
  * the staff Subscriber Cleanup page -- review one screen of flagged rows and
    either delete them or mark them reviewed (kept)

Every finding carries a reason code (see :data:`REASONS`). Reasons are split
into two buckets:

  * ``auto_cleanable`` -- unambiguous junk (unmailable, malformed, URL-in-name
    spam, header-injection attempts). None of these rows ever received mail a
    person could act on, so deleting them breaks no unsubscribe link. Bulk
    deletion targets only these.
  * review-only -- a *signal*, not a verdict (duplicate and alias-duplicate,
    spam-associated TLD, internal test address, role account, link or mail
    header in comments, suspicious unicode). A human decides; nothing bulk-deletes these.

The same analysis runs over the other inbound-contact tables (chat leads, demo
requests, interested professionals) so staff can *see* suspicious rows there,
but this flow never deletes from them -- those are sales/lead records with their
own workflows (see ``proconnector.py``).
"""

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from loguru import logger

from fighthealthinsurance.chat.message_preprocessor import has_suspicious_unicode
from fighthealthinsurance.email_utils import (
    INTERNAL_TEST_EMAILS,
    SPAM_EMAIL_TLDS,
    get_email_domain,
    is_blocked_email,
)
from fighthealthinsurance.models import (
    ChatLeads,
    DemoRequests,
    InterestedProfessional,
    MailingListSubscriber,
)
from fighthealthinsurance.utils import mask_email_for_logging


@dataclass(frozen=True)
class Reason:
    """One kind of problem we can find on a contact row.

    ``auto_cleanable`` means "safe to delete in bulk without a human looking at
    the individual row". Everything else is a review signal only.
    """

    code: str
    label: str
    description: str
    auto_cleanable: bool


UNSENDABLE = "unsendable_email"
INVALID_EMAIL = "invalid_email"
DUPLICATE = "duplicate_email"
LINK_IN_FIELDS = "link_in_fields"
LINK_IN_COMMENTS = "link_in_comments"
HEADER_IN_COMMENTS = "header_in_comments"
HEADER_INJECTION = "header_injection"
CONTROL_IN_EMAIL = "control_chars_in_email"
SPAM_TLD = "spam_tld"
INTERNAL_TEST = "internal_test_address"
ROLE_ADDRESS = "role_address"
ALIAS_DUPLICATE = "alias_duplicate"
SUSPICIOUS_UNICODE = "suspicious_unicode"
OVERLONG_FIELD = "overlong_field"
PRO_SPAM_RULES = "pro_signup_spam_rules"

REASONS: Dict[str, Reason] = {
    r.code: r
    for r in (
        Reason(
            UNSENDABLE,
            "Unmailable address",
            "Blocked/reserved domain or malformed address -- mail to it can never "
            "be delivered (see email_utils.is_blocked_email).",
            auto_cleanable=True,
        ),
        Reason(
            INVALID_EMAIL,
            "Invalid address",
            "Fails Django's email validation, so it was never a working mailbox.",
            auto_cleanable=True,
        ),
        Reason(
            DUPLICATE,
            "Duplicate address",
            "Another row holds the same address (case-insensitively). Harmless "
            "as it stands: sends and unsubscribes already treat the address as "
            "one person. Deleting a copy retires the unsubscribe link already "
            "mailed from that row, so Keep is usually the right call.",
            auto_cleanable=False,
        ),
        Reason(
            LINK_IN_FIELDS,
            "URL / markup in name or phone",
            "Links or BBCode/HTML in a name or phone field: the signature of an "
            "automated form-spam submission.",
            auto_cleanable=True,
        ),
        Reason(
            LINK_IN_COMMENTS,
            "URL / markup in comments",
            "A link in the comments field. No signup form writes free text there "
            '(our code stores fixed notes like "From appeal flow"), so this came '
            "from an admin edit or an import -- worth a look, not a bulk delete.",
            auto_cleanable=False,
        ),
        Reason(
            HEADER_INJECTION,
            "Header-injection attempt",
            "A line break or a mail header (bcc:, content-type:) in a name or "
            "phone field -- single-line form inputs, so this came from a crafted "
            "request trying to inject headers into outgoing mail.",
            auto_cleanable=True,
        ),
        Reason(
            HEADER_IN_COMMENTS,
            "Mail header in comments",
            "A line in the comments field starts like a mail header (bcc:, "
            "content-type:). Comments are written by our code or by staff, not "
            "by signup forms, so this is worth a look, not a bulk delete.",
            auto_cleanable=False,
        ),
        Reason(
            CONTROL_IN_EMAIL,
            "Control characters in address",
            "The address itself contains control/zero-width characters.",
            auto_cleanable=True,
        ),
        Reason(
            SPAM_TLD,
            "Spam-associated TLD",
            f"Address on one of {', '.join(SPAM_EMAIL_TLDS)} -- treated as spam "
            "or out of scope elsewhere in the codebase.",
            auto_cleanable=False,
        ),
        Reason(
            INTERNAL_TEST,
            "Internal / test address",
            "One of our own test accounts or an obvious joke signup.",
            auto_cleanable=False,
        ),
        Reason(
            ROLE_ADDRESS,
            "Role account",
            "postmaster@, noreply@, abuse@ and friends: nobody reads these, and "
            "mailing them hurts sender reputation.",
            auto_cleanable=False,
        ),
        Reason(
            ALIAS_DUPLICATE,
            "Possible alias duplicate",
            "Another row normalizes to the same mailbox (gmail dots, +tag "
            "addressing). Usually the same person twice, but not certain enough "
            "to delete automatically.",
            auto_cleanable=False,
        ),
        Reason(
            SUSPICIOUS_UNICODE,
            "Suspicious unicode",
            "Zero-width, bidi-override or control characters in a field -- used "
            "to disguise spam text.",
            auto_cleanable=False,
        ),
        Reason(
            OVERLONG_FIELD,
            "Implausibly long field",
            "A name or phone far longer than any real value, typically a pasted "
            "spam payload.",
            auto_cleanable=False,
        ),
        Reason(
            PRO_SPAM_RULES,
            "Matches pro-signup spam rules",
            "Trips the professional-intake spam rules (pro_signups.spam_reason: "
            "SEO/backlink/crypto pitches, links in the name, ...). Interested "
            "professionals only.",
            auto_cleanable=False,
        ),
    )
}

AUTO_CLEANABLE_CODES: frozenset[str] = frozenset(
    code for code, reason in REASONS.items() if reason.auto_cleanable
)

# Markers that mean "there is a link here". Checked against the *name*, *phone*
# and *comments* fields, never the email address. Our own code only ever writes
# fixed strings like "From appeal flow" into comments, so any link there came
# from a bot. "http" is deliberately bare (it catches http:// and https:// and
# obfuscated variants) and matches the existing pro-connector spam filter.
_LINK_MARKERS: Tuple[str, ...] = (
    "http",
    "www.",
    "[url",
    "[/url]",
    "<a ",
    "href=",
    "://",
)

# A mail header at the start of a field or of any line in it. Anchoring on a
# line start is what separates an injection payload ("Jane\nBcc: x@y") from
# ordinary prose that merely contains "cc:" ("please cc: my office manager").
_HEADER_LINE = re.compile(
    r"(?:^|[\r\n])[ \t]*(bcc|cc|to|content-type|mime-version"
    r"|content-transfer-encoding)[ \t]*:",
    re.IGNORECASE,
)

# Fields that are single-line by construction (a form text input), so a CR/LF
# in them can only have come from a crafted request. Comments are excluded:
# lead tables store multi-line messages there.
_SINGLE_LINE_FIELDS: frozenset[str] = frozenset({"name", "phone"})

# Local parts that are automated mailboxes rather than people.
_ROLE_LOCAL_PARTS: frozenset[str] = frozenset(
    {
        "abuse",
        "admin",
        "administrator",
        "bounce",
        "bounces",
        "donotreply",
        "do-not-reply",
        "hostmaster",
        "mailer-daemon",
        "no-reply",
        "noreply",
        "postmaster",
        "root",
        "webmaster",
    }
)

# Local parts that mean somebody was testing the form.
_TEST_LOCAL_PARTS: frozenset[str] = frozenset(
    {"test", "testing", "test1", "test123", "asdf", "aaaa", "qwerty"}
)

# Domains where a "." in the local part is not significant, so
# ``f.oo@gmail.com`` and ``foo@gmail.com`` are the same mailbox.
_DOT_INSENSITIVE_DOMAINS: frozenset[str] = frozenset({"gmail.com", "googlemail.com"})

# Longest plausible values; anything past these is a pasted payload, not a name.
_MAX_NAME_LEN = 200
_MAX_PHONE_LEN = 40

# How many sibling row ids an alias-duplicate finding names before summarizing.
_ALIAS_SAMPLE = 5


@dataclass(frozen=True)
class ContactRecord:
    """A normalized view of one inbound-contact row, whatever table it came from.

    Keeping the analysis on this small struct (rather than on a model) is what
    lets the same checks run over subscribers, chat leads, demo requests and
    interested professionals.
    """

    pk: int
    email: str
    name: str = ""
    phone: str = ""
    comments: str = ""
    created: Optional[date] = None
    source: str = "MailingListSubscriber"
    # What ``comments`` holds in the source table, for evidence strings (the
    # lead tables map other free-text columns, e.g. ``company``, onto it).
    comments_label: str = "comments"


@dataclass
class Finding:
    """Everything we found wrong with a single row."""

    record: ContactRecord
    codes: List[str] = field(default_factory=list)
    # code -> short human-readable evidence ("duplicate of #12", "name contains 'http'")
    details: Dict[str, str] = field(default_factory=dict)

    def add(self, code: str, detail: str = "") -> None:
        if code not in self.codes:
            self.codes.append(code)
        if detail:
            self.details[code] = detail

    @property
    def auto_cleanable(self) -> bool:
        """True when at least one reason is safe to bulk-delete on."""
        return any(code in AUTO_CLEANABLE_CODES for code in self.codes)

    @property
    def reasons(self) -> List[Reason]:
        return [REASONS[code] for code in self.codes]

    def describe(self) -> str:
        """One-line summary for CLI output. Never includes the raw address."""
        parts = []
        for code in self.codes:
            detail = self.details.get(code)
            parts.append(f"{code} ({detail})" if detail else code)
        return (
            f"#{self.record.pk} {mask_email_for_logging(self.record.email)}: "
            + ", ".join(parts)
        )


@dataclass
class ScanResult:
    """Findings for one table, plus the counts needed to render a summary."""

    source: str
    scanned: int
    findings: List[Finding] = field(default_factory=list)

    @property
    def flagged_count(self) -> int:
        return len(self.findings)

    @property
    def auto_cleanable_findings(self) -> List[Finding]:
        return [f for f in self.findings if f.auto_cleanable]

    def counts_by_code(self) -> Dict[str, int]:
        """How many rows carry each reason, ordered by :data:`REASONS`."""
        counts = {code: 0 for code in REASONS}
        for finding in self.findings:
            for code in finding.codes:
                counts[code] += 1
        return {code: count for code, count in counts.items() if count}

    def findings_with_code(self, code: str) -> List[Finding]:
        return [f for f in self.findings if code in f.codes]


def normalized_email(email: str) -> str:
    """Lowercased/trimmed address -- our notion of "the same subscriber"."""
    return (email or "").strip().lower()


def mailbox_key(email: str) -> str:
    """Best-effort "same mailbox" key: drops +tags, and dots on gmail.

    Used only to *suggest* alias duplicates for human review; it is deliberately
    never used to delete, because +tag addressing is legitimately distinct at
    some providers.
    """
    normalized = normalized_email(email)
    domain = get_email_domain(normalized)
    if not domain:
        return normalized
    local = normalized.rsplit("@", 1)[0]
    local = local.split("+", 1)[0]
    if domain in _DOT_INSENSITIVE_DOMAINS:
        local = local.replace(".", "")
    return f"{local}@{domain}"


def _contains(text: str, markers: Sequence[str]) -> Optional[str]:
    """Return the first marker present in ``text`` (case-insensitively)."""
    lowered = (text or "").lower()
    for marker in markers:
        if marker in lowered:
            return marker
    return None


def _check_email(record: ContactRecord, finding: Finding) -> None:
    email = record.email or ""
    stripped = email.strip()

    if any(ch in email for ch in ("\n", "\r", "\t")) or has_suspicious_unicode(email):
        finding.add(CONTROL_IN_EMAIL, "control/zero-width characters in address")

    if is_blocked_email(stripped):
        domain = get_email_domain(stripped)
        finding.add(UNSENDABLE, f"domain {domain}" if domain else "unparseable address")
    else:
        try:
            validate_email(stripped)
        except ValidationError:
            finding.add(INVALID_EMAIL, "fails email validation")

    lowered = normalized_email(email)
    if lowered in INTERNAL_TEST_EMAILS:
        finding.add(INTERNAL_TEST, "known internal/test address")
    elif "@" in lowered:
        local = lowered.rsplit("@", 1)[0]
        if local in _TEST_LOCAL_PARTS:
            finding.add(INTERNAL_TEST, f"test-looking local part {local}@")
        if local in _ROLE_LOCAL_PARTS:
            finding.add(ROLE_ADDRESS, f"role mailbox {local}@")

    if lowered.endswith(SPAM_EMAIL_TLDS):
        finding.add(SPAM_TLD, f"TLD {lowered.rsplit('.', 1)[-1]}")


def _check_text_fields(record: ContactRecord, finding: Finding) -> None:
    fields = (
        ("name", record.name),
        ("phone", record.phone),
        (record.comments_label, record.comments),
    )
    for label, value in fields:
        if not value:
            continue
        marker = _contains(value, _LINK_MARKERS)
        if marker:
            code = LINK_IN_FIELDS if label in _SINGLE_LINE_FIELDS else LINK_IN_COMMENTS
            finding.add(code, f"{label} contains {marker!r}")
        header = _HEADER_LINE.search(value)
        if header:
            # Only the single-line form inputs are reachable by a signup bot;
            # comments are written by our code or staff, so there it's a signal.
            code = (
                HEADER_INJECTION if label in _SINGLE_LINE_FIELDS else HEADER_IN_COMMENTS
            )
            finding.add(code, f"{label} contains a {header.group(1)}: header")
        elif label in _SINGLE_LINE_FIELDS and ("\n" in value or "\r" in value):
            finding.add(HEADER_INJECTION, f"{label} contains a newline")
        if has_suspicious_unicode(value):
            finding.add(SUSPICIOUS_UNICODE, f"{label} has hidden/bidi characters")

    if len(record.name or "") > _MAX_NAME_LEN:
        finding.add(OVERLONG_FIELD, f"name is {len(record.name)} chars")
    if len(record.phone or "") > _MAX_PHONE_LEN:
        finding.add(OVERLONG_FIELD, f"phone is {len(record.phone)} chars")


def analyze_record(record: ContactRecord) -> Finding:
    """Run every single-row check. Cross-row checks live in :func:`scan_records`."""
    finding = Finding(record=record)
    _check_email(record, finding)
    _check_text_fields(record, finding)
    return finding


def _add_duplicate_findings(
    records: Sequence[ContactRecord], findings: Dict[int, Finding]
) -> None:
    """Flag exact duplicates and alias duplicates (both review-only).

    For exact duplicates the row to keep is the newest one that is not itself
    junk: the signup paths look up the newest row for an address and update it
    in place, so it holds the freshest name/referral data -- unless a bot later
    reused the address, in which case that newest row is the junk and the
    person's own older row is the one to keep. Must run after the single-row
    checks, which it reads from ``findings``.
    """
    by_email: Dict[str, List[ContactRecord]] = {}
    by_mailbox: Dict[str, List[ContactRecord]] = {}
    for record in records:
        email = normalized_email(record.email)
        if not email:
            continue
        by_email.setdefault(email, []).append(record)
        by_mailbox.setdefault(mailbox_key(record.email), []).append(record)

    for group in by_email.values():
        if len(group) < 2:
            continue
        ordered = sorted(group, key=lambda r: r.pk)
        clean = [
            r
            for r in ordered
            if r.pk not in findings or not findings[r.pk].auto_cleanable
        ]
        keeper = (clean or ordered)[-1]
        for record in ordered:
            if record.pk == keeper.pk:
                continue
            relation = "newer" if keeper.pk > record.pk else "older"
            findings.setdefault(record.pk, Finding(record=record)).add(
                DUPLICATE, f"same address as {relation} row #{keeper.pk}"
            )

    for group in by_mailbox.values():
        distinct = {normalized_email(r.email) for r in group}
        if len(distinct) < 2:
            # Either a single row or an exact-duplicate group already handled above.
            continue
        # A bot can mint thousands of +tag variants of one inbox, so name a
        # few siblings and count the rest rather than listing every one on
        # every row (which grows with the square of the group).
        pks = sorted(r.pk for r in group)
        for record in group:
            sample = [pk for pk in pks[: _ALIAS_SAMPLE + 1] if pk != record.pk]
            sample = sample[:_ALIAS_SAMPLE]
            detail = "same mailbox as " + ", ".join(f"#{pk}" for pk in sample)
            if len(pks) - 1 > len(sample):
                detail += f" and {len(pks) - 1 - len(sample)} more"
            findings.setdefault(record.pk, Finding(record=record)).add(
                ALIAS_DUPLICATE, detail
            )


def scan_records(
    records: Sequence[ContactRecord], source: str = "MailingListSubscriber"
) -> ScanResult:
    """Analyze a batch of rows, including the cross-row duplicate checks.

    Duplicate detection is only correct over a *complete* set of rows, so pass
    the whole table (or a whole address-space slice of it), not a page.
    """
    findings: Dict[int, Finding] = {}
    for record in records:
        finding = analyze_record(record)
        if finding.codes:
            findings[record.pk] = finding
    _add_duplicate_findings(records, findings)
    ordered = sorted(findings.values(), key=lambda f: f.record.pk)
    return ScanResult(source=source, scanned=len(records), findings=ordered)


def subscriber_records(
    queryset: Optional["QuerySet[MailingListSubscriber]"] = None,
) -> Tuple[List[ContactRecord], List[int]]:
    """Load mailing list subscribers as records, plus the reviewed-row ids.

    Reviewed rows are returned too -- they are only dropped *after* the scan (see
    :func:`scan_subscribers`), because removing them first would hide the fact
    that an unreviewed row duplicates a reviewed one, leaving the older copy
    looking unique and surviving cleanup.
    """
    qs = MailingListSubscriber.objects.all() if queryset is None else queryset
    records: List[ContactRecord] = []
    reviewed: List[int] = []
    for sub in qs.iterator():
        if sub.cleanup_reviewed_at is not None:
            reviewed.append(sub.id)
        records.append(
            ContactRecord(
                pk=sub.id,
                email=sub.email or "",
                name=sub.name or "",
                phone=sub.phone or "",
                comments=sub.comments or "",
                created=sub.signup_date,
                source="MailingListSubscriber",
            )
        )
    return records, reviewed


def scan_subscribers(
    queryset: Optional["QuerySet[MailingListSubscriber]"] = None,
    include_reviewed: bool = False,
) -> ScanResult:
    """Scan the mailing list. This is the table the cleanup flow can delete from.

    Rows staff already reviewed and chose to keep are scanned (so duplicate
    detection stays correct) but reported only with ``include_reviewed``.
    """
    records, reviewed = subscriber_records(queryset=queryset)
    result = scan_records(records, source="MailingListSubscriber")
    if include_reviewed:
        return result
    reviewed_ids = set(reviewed)
    return ScanResult(
        source=result.source,
        scanned=result.scanned - len(reviewed_ids),
        findings=[f for f in result.findings if f.record.pk not in reviewed_ids],
    )


def _lead_record(obj: Any, source: str, **field_map: str) -> ContactRecord:
    """Map one lead-table row onto a ContactRecord via ``field_map`` renames."""

    def value(key: str, default: str) -> Any:
        return getattr(obj, field_map.get(key, default), None)

    return ContactRecord(
        pk=obj.pk,
        email=getattr(obj, "email", "") or "",
        name=value("name", "name") or "",
        phone=value("phone", "phone") or "",
        comments=value("comments", "comments") or "",
        created=value("created", "signup_date"),
        source=source,
        comments_label=field_map.get("comments", "comments"),
    )


def _lead_records(model: Any, source: str, **field_map: str) -> List[ContactRecord]:
    return [
        _lead_record(obj, source, **field_map) for obj in model.objects.all().iterator()
    ]


def scan_other_contact_tables() -> List[ScanResult]:
    """Report-only scans of the other inbound-contact tables.

    Nothing in this flow deletes from these -- they are sales/lead records with
    their own workflows -- but the same signals are worth surfacing.
    """
    results = []
    results.append(
        scan_records(
            _lead_records(
                ChatLeads, "ChatLeads", comments="company", created="created_at"
            ),
            source="ChatLeads",
        )
    )
    results.append(
        scan_records(
            _lead_records(DemoRequests, "DemoRequests", comments="company"),
            source="DemoRequests",
        )
    )
    results.append(_scan_interested_professionals())
    return results


def _scan_interested_professionals() -> ScanResult:
    """Generic checks plus the professional-intake spam rules.

    Professional signups already have a dedicated spam classifier
    (``pro_signups.spam_reason``) that new submissions pass through at intake;
    reusing it here surfaces older rows that predate it, or that were let
    through, with the same verdict staff see elsewhere.
    """
    # Imported lazily: pro_signups pulls in the email-sending stack, which the
    # mailing-list cleanup path never needs.
    from fighthealthinsurance.pro_signups import spam_reason

    source = "InterestedProfessional"
    records: List[ContactRecord] = []
    spam: Dict[int, str] = {}
    for pro in InterestedProfessional.objects.all().iterator():
        records.append(_lead_record(pro, source, phone="phone_number"))
        reason = spam_reason(pro)
        if reason:
            spam[pro.pk] = reason

    result = scan_records(records, source=source)
    findings = {f.record.pk: f for f in result.findings}
    by_pk = {r.pk: r for r in records}
    for pk, reason in spam.items():
        findings.setdefault(pk, Finding(record=by_pk[pk])).add(PRO_SPAM_RULES, reason)
    result.findings = sorted(findings.values(), key=lambda f: f.record.pk)
    return result


def delete_subscribers(
    ids: Iterable[int], actor: str = "", unreviewed_only: bool = False
) -> int:
    """Delete mailing list subscribers by id. Returns the number deleted.

    Deletion is the correct "cleanup" here: unsubscribing is already modelled as
    deleting the row (see UnsubscribeView), and RemoveDataHelper deletes these
    rows too, so there is no soft-delete state to respect.

    ``unreviewed_only`` re-checks the review state at delete time, under a row
    lock, so a row staff marked "keep" after the caller's scan is spared. The
    staff page always sets it; the command sets it unless --include-reviewed.
    """
    id_list = list(ids)
    if not id_list:
        return 0
    with transaction.atomic():
        qs = MailingListSubscriber.objects.select_for_update().filter(id__in=id_list)
        if unreviewed_only:
            qs = qs.filter(cleanup_reviewed_at__isnull=True)
        rows = list(qs)
        # Log what goes away (masked) so a bad cleanup is at least reconstructible.
        for sub in rows:
            logger.info(
                f"Subscriber cleanup: deleting #{sub.id} "
                f"{mask_email_for_logging(sub.email)}"
                + (f" (by {actor})" if actor else "")
            )
        deleted, _ = MailingListSubscriber.objects.filter(
            id__in=[sub.id for sub in rows]
        ).delete()
    return deleted


def mark_subscribers_reviewed(ids: Iterable[int], actor: str = "") -> int:
    """Mark rows as reviewed-and-kept so they leave the cleanup queue."""
    id_list = list(ids)
    if not id_list:
        return 0
    # Only rows nobody has reviewed yet: a second Keep on the same row (two
    # staff, or a double submit) must not overwrite who reviewed it and when.
    return MailingListSubscriber.objects.filter(
        id__in=id_list, cleanup_reviewed_at__isnull=True
    ).update(
        cleanup_reviewed_at=timezone.now(), cleanup_reviewed_by=(actor or "")[:150]
    )
