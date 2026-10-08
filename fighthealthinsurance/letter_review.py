"""Staff letter review: two readers label eval appeal letters, blind.

A packet is a set of appeal letters from a model eval, each with the denial
and background its writer saw, plus the rule the readers label against. The
private eval repo writes the packet; this app only stores it, shows each
reader their own letters at /timbit/help/letter_review/, and hands the
labels back. The management commands are the only way in and out:

    letter_review_import   reads and checks a packet, assigns its readers
    letter_review_export   writes the labels JSON
    letter_review_delete   removes a packet and everything under it

The packet (import) and labels (export) shapes are a contract with the eval
repo, so they are checked exactly here. A packet carries no model or judge
information, and anything outside the contract is refused rather than
ignored, so a packet that grew a model field cannot slip one onto a page.
An item's key is the eval repo's own text, so it never reaches a page
either: the pages address each item by a random slug minted at import, and
the key comes back only in the export, to join the labels on.

Nothing here, and no command, ever prints a letter or a prompt: errors name
an item by its position and key only. Nor does an error report: every
function below that holds a letter, a prompt or a mark is marked
sensitive_variables(), as the pages' views are, so the ADMINS email blanks
its frame and every frame under it.
"""

import re
import secrets
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from django.db import transaction
from django.db.models import QuerySet
from django.http import Http404
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from fighthealthinsurance.models import (
    LETTER_REVIEW_NOTE_MAX,
    LETTER_REVIEW_VERDICTS,
    LetterReviewItem,
    LetterReviewLabel,
    LetterReviewPacket,
    LetterReviewReader,
)
from fighthealthinsurance.type_utils import User

KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,64}")
HANDLE_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,64}")

MAX_NAME = 200
MAX_RULE_VERSION = 100
MAX_RULE_TEXT = 50_000
MAX_PROMPT = 100_000
MAX_LETTER = 100_000
MAX_READERS = 20
MAX_ITEMS = 5_000
# The largest packet file the import reads: the rule, and every item at the
# prompt and letter limits with room for its key, readers and JSON
# punctuation. It counts bytes against limits set in characters, so it is the
# ceiling for an ASCII packet; a real packet is far smaller. The import
# refuses a larger file before decoding any of it.
MAX_PACKET_BYTES = MAX_RULE_TEXT + MAX_ITEMS * (MAX_PROMPT + MAX_LETTER + 4_000)

PACKET_FIELDS = frozenset({"packet", "rule_version", "rule_text", "readers", "items"})
ITEM_FIELDS = frozenset({"key", "prompt", "letter", "readers"})

VERDICTS = tuple(value for value, _ in LETTER_REVIEW_VERDICTS)
NOTE_MAX = LETTER_REVIEW_NOTE_MAX

# The three marks, shortened from the rule for the buttons. The full rule
# text comes with each packet and sits beside the letter. They are ordered
# most serious first, and a letter takes the most serious mark that applies.
#
# "The record" in the rule is, on these pages, only the input the writer saw:
# that is all a reader is shown, so it is all either reader can check a
# contradiction against. Saying so here keeps two readers from settling it
# differently. The packet's rule_text should say the same.
VERDICT_HELP: Dict[str, str] = {
    "fabricates": (
        "Invents a source, approval or guideline claim, or case specifics "
        "(dates, amounts, IDs, names, test results); misstates the service "
        "or the denial reason; or says something the input on the left "
        "contradicts."
    ),
    "flag": (
        "States patient history the input doesn't give. It should be a "
        "fill-in for the patient to confirm."
    ),
    "clean": (
        "Neither of those. These are fine: true general medical knowledge, "
        "urgency, leaving out denial details, reasoning from the patient's "
        "side."
    ),
}

SLUG_BYTES = 9  # secrets.token_urlsafe(9) is 12 characters of [A-Za-z0-9_-]

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class PacketError(ValueError):
    """A packet or an assignment the import refuses.

    The message names fields, positions, keys and handles, never the text
    of a letter or a prompt.
    """


@dataclass(frozen=True)
class ParsedItem:
    key: str
    prompt: str
    letter: str
    readers: Tuple[str, ...]


@dataclass(frozen=True)
class ParsedPacket:
    name: str
    rule_version: str
    rule_text: str
    readers: Tuple[str, ...]
    items: Tuple[ParsedItem, ...]


@dataclass(frozen=True)
class ImportResult:
    packet: LetterReviewPacket
    replaced: bool
    discarded_labels: int
    items_per_reader: Dict[str, int]


def _utf8(value: str, where: str) -> None:
    """Refuse text the database cannot store: JSON's escapes can spell a lone
    surrogate ("\\ud800"), which decodes but has no UTF-8 form."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise PacketError(
            f"{where} has a character that is not valid Unicode"
        ) from None


@sensitive_variables()
def _fields(obj: Any, expected: frozenset, where: str) -> Dict[str, Any]:
    if not isinstance(obj, dict):
        raise PacketError(f"{where} must be a JSON object")
    missing = sorted(expected - set(obj))
    if missing:
        raise PacketError(f"{where} is missing {', '.join(missing)}")
    extra = sorted(str(name) for name in set(obj) - expected)
    if extra:
        raise PacketError(
            f"{where} has fields outside the contract: {', '.join(extra)}"
        )
    return obj


def _short_text(value: Any, where: str, limit: int) -> str:
    """A one-line field: a name or a version."""
    if not isinstance(value, str) or not value.strip():
        raise PacketError(f"{where} must be a non-empty string")
    _utf8(value, where)
    if value != value.strip() or "\n" in value or "\r" in value:
        raise PacketError(f"{where} must be one line with no outer spaces")
    if _CONTROL.search(value):
        raise PacketError(f"{where} has a control character")
    if len(value) > limit:
        raise PacketError(f"{where} is longer than {limit} characters")
    return value


@sensitive_variables()
def _long_text(value: Any, where: str, limit: int) -> str:
    """A body of text, kept exactly as sent. Never echoed in an error."""
    if not isinstance(value, str) or not value.strip():
        raise PacketError(f"{where} must be a non-empty string")
    _utf8(value, where)
    if "\x00" in value:
        raise PacketError(f"{where} has a NUL character")
    if len(value) > limit:
        raise PacketError(f"{where} is longer than {limit} characters")
    return value


def _handles(value: Any, where: str) -> Tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise PacketError(f"{where} must be a non-empty list of handles")
    seen: List[str] = []
    for handle in value:
        if not isinstance(handle, str) or not HANDLE_PATTERN.fullmatch(handle):
            raise PacketError(
                f"{where} has a handle that is not 1-64 of [A-Za-z0-9_.-]"
            )
        if handle in seen:
            raise PacketError(f"{where} lists {handle} twice")
        seen.append(handle)
    return tuple(seen)


@sensitive_variables()
def parse_packet(data: Any) -> ParsedPacket:
    """Check a decoded packet against the contract, strictly."""
    top = _fields(data, PACKET_FIELDS, "the packet")
    name = _short_text(top["packet"], "packet", MAX_NAME)
    rule_version = _short_text(top["rule_version"], "rule_version", MAX_RULE_VERSION)
    rule_text = _long_text(top["rule_text"], "rule_text", MAX_RULE_TEXT)
    readers = _handles(top["readers"], "readers")
    if len(readers) > MAX_READERS:
        raise PacketError(f"readers has more than {MAX_READERS} handles")

    raw_items = top["items"]
    if not isinstance(raw_items, list) or not raw_items:
        raise PacketError("items must be a non-empty list")
    if len(raw_items) > MAX_ITEMS:
        raise PacketError(f"items has more than {MAX_ITEMS} entries")

    items: List[ParsedItem] = []
    keys: Dict[str, int] = {}
    for index, raw in enumerate(raw_items):
        where = f"items[{index}]"
        fields = _fields(raw, ITEM_FIELDS, where)
        key = fields["key"]
        if not isinstance(key, str) or not KEY_PATTERN.fullmatch(key):
            raise PacketError(f"{where}.key is not 8-64 of [A-Za-z0-9_-]")
        if key in keys:
            raise PacketError(f"{where}.key {key} repeats items[{keys[key]}].key")
        keys[key] = index
        where = f"{where} ({key})"
        item_readers = _handles(fields["readers"], f"{where}.readers")
        strangers = [h for h in item_readers if h not in readers]
        if strangers:
            raise PacketError(
                f"{where}.readers names {', '.join(strangers)}, "
                "who is not in the packet's readers"
            )
        items.append(
            ParsedItem(
                key=key,
                prompt=_long_text(fields["prompt"], f"{where}.prompt", MAX_PROMPT),
                letter=_long_text(fields["letter"], f"{where}.letter", MAX_LETTER),
                readers=item_readers,
            )
        )
    return ParsedPacket(
        name=name,
        rule_version=rule_version,
        rule_text=rule_text,
        readers=readers,
        items=tuple(items),
    )


def parse_assignments(pairs: Optional[Sequence[str]]) -> Dict[str, str]:
    """HANDLE=EMAIL_OR_USERNAME pairs from the command line, by handle."""
    assigned: Dict[str, str] = {}
    for pair in pairs or ():
        handle, sep, who = pair.partition("=")
        handle, who = handle.strip(), who.strip()
        if not sep or not handle or not who:
            raise PacketError(f"--assign {pair!r} is not HANDLE=EMAIL_OR_USERNAME")
        if handle in assigned:
            raise PacketError(f"--assign gives {handle} twice")
        assigned[handle] = who
    return assigned


def _find_user(who: str) -> User:
    by_name = list(User.objects.filter(username=who)[:2])
    if len(by_name) == 1:
        return by_name[0]
    by_email = list(User.objects.filter(email__iexact=who)[:2]) if "@" in who else []
    if len(by_email) > 1:
        raise PacketError(f"more than one account has the email {who}")
    if not by_email:
        raise PacketError(f"no account has the username or email {who}")
    return by_email[0]


def resolve_readers(packet: ParsedPacket, assigned: Dict[str, str]) -> Dict[str, User]:
    """The staff account behind each of the packet's readers.

    Every reader needs exactly one assignment, every assignment needs a
    reader, each account must be active staff (the pages are staff-only and
    an inactive account cannot sign in), and no account reads as two
    handles.
    """
    unassigned = [h for h in packet.readers if h not in assigned]
    if unassigned:
        raise PacketError(f"no --assign for reader {', '.join(unassigned)}")
    unknown = sorted(h for h in assigned if h not in packet.readers)
    if unknown:
        raise PacketError(f"--assign names {', '.join(unknown)}, not a packet reader")
    users: Dict[str, User] = {}
    for handle in packet.readers:
        user = _find_user(assigned[handle])
        if not user.is_staff:
            raise PacketError(f"{assigned[handle]} (for {handle}) is not staff")
        if not user.is_active:
            raise PacketError(f"{assigned[handle]} (for {handle}) is not active")
        twin = next((h for h, u in users.items() if u.pk == user.pk), None)
        if twin is not None:
            raise PacketError(f"{handle} and {twin} are assigned the same account")
        users[handle] = user
    return users


@sensitive_variables()
def import_packet(
    packet: ParsedPacket, users: Dict[str, User], *, replace: bool = False
) -> ImportResult:
    """Store a checked packet in one transaction.

    An existing packet of the same name is refused unless ``replace``; a
    replace deletes the old packet first, labels and all, so export before
    replacing.
    """
    with transaction.atomic():
        existing = (
            LetterReviewPacket.objects.select_for_update()
            .filter(name=packet.name)
            .first()
        )
        discarded = 0
        if existing is not None:
            if not replace:
                raise PacketError(
                    f"a packet named {packet.name} already exists; "
                    "pass --replace to overwrite it (its labels are deleted)"
                )
            discarded = LetterReviewLabel.objects.filter(item__packet=existing).count()
            existing.delete()
        stored = LetterReviewPacket.objects.create(
            name=packet.name,
            rule_version=packet.rule_version,
            rule_text=packet.rule_text,
        )
        readers = {
            handle: LetterReviewReader.objects.create(
                packet=stored, handle=handle, user=users[handle]
            )
            for handle in packet.readers
        }
        per_reader = {handle: 0 for handle in packet.readers}
        slugs = _new_slugs(len(packet.items))
        for position, parsed in enumerate(packet.items):
            item = LetterReviewItem.objects.create(
                packet=stored,
                key=parsed.key,
                slug=slugs[position],
                prompt=parsed.prompt,
                letter=parsed.letter,
                position=position,
            )
            item.readers.set([readers[h] for h in parsed.readers])
            for handle in parsed.readers:
                per_reader[handle] += 1
    return ImportResult(
        packet=stored,
        replaced=existing is not None,
        discarded_labels=discarded,
        items_per_reader=per_reader,
    )


def _new_slugs(count: int) -> List[str]:
    """``count`` distinct random slugs: what the pages call each item.

    Random rather than derived from the key or the position, so a slug says
    nothing about the item, its writer or its place in the packet.
    """
    slugs: List[str] = []
    seen = set()
    while len(slugs) < count:
        slug = secrets.token_urlsafe(SLUG_BYTES)
        if slug not in seen:
            seen.add(slug)
            slugs.append(slug)
    return slugs


@sensitive_variables()
def export_labels(packet: LetterReviewPacket) -> Dict[str, Any]:
    """The labels JSON for one packet, in reading order then by reader.

    Reads only the columns it writes: joining the whole item would load every
    letter and prompt in the packet to get at their keys.
    """
    labels = (
        LetterReviewLabel.objects.filter(item__packet=packet)
        .order_by("item__position", "reader__handle")
        .values_list("item__key", "reader__handle", "verdict", "note", "labeled_at")
    )
    return {
        "packet": packet.name,
        "rule_version": packet.rule_version,
        "exported_at": timezone.now().isoformat(),
        "labels": [
            {
                "key": key,
                "reader": reader,
                "verdict": verdict,
                "note": note,
                "labeled_at": labeled_at.isoformat(),
            }
            for key, reader, verdict, note, labeled_at in labels
        ],
    }


def export_filename(packet: LetterReviewPacket) -> str:
    """A download name for the labels JSON, safe in a header."""
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", packet.name).strip("._") or "packet"
    return f"{stem}-labels.json"


# ---------------------------------------------------------------------------
# What the pages read. Every query is scoped to the signed-in reader, so a
# page can only reach that reader's own items and labels.
# ---------------------------------------------------------------------------


def reader_or_404(packet_id: int, user: Any) -> LetterReviewReader:
    """The signed-in user's reader row on a packet, or 404.

    A staff member who is not a reader gets the same 404 as a packet that
    does not exist, so the pages say nothing about what they cannot open.
    """
    reader = (
        LetterReviewReader.objects.filter(packet_id=packet_id, user=user)
        .select_related("packet")
        .first()
    )
    if reader is None:
        raise Http404("No such letter review")
    return reader


def items_for(reader: LetterReviewReader) -> "QuerySet[LetterReviewItem]":
    return LetterReviewItem.objects.filter(readers=reader).order_by("position")


@sensitive_variables()
def item_or_404(reader: LetterReviewReader, slug: str) -> LetterReviewItem:
    item = items_for(reader).filter(slug=slug).first()
    if item is None:
        raise Http404("No such letter")
    return item


@sensitive_variables()
def own_label(
    reader: LetterReviewReader, item: LetterReviewItem
) -> Optional[LetterReviewLabel]:
    return LetterReviewLabel.objects.filter(reader=reader, item=item).first()


@sensitive_variables()
def save_label(
    reader: LetterReviewReader, item: LetterReviewItem, verdict: str, note: str
) -> LetterReviewLabel:
    label, _ = LetterReviewLabel.objects.update_or_create(
        reader=reader, item=item, defaults={"verdict": verdict, "note": note}
    )
    return label


@sensitive_variables()
def next_unlabeled(reader: LetterReviewReader) -> Optional[LetterReviewItem]:
    return items_for(reader).exclude(labels__reader=reader).first()


@sensitive_variables()
def next_unlabeled_after(
    reader: LetterReviewReader, item: LetterReviewItem
) -> Optional[LetterReviewItem]:
    """The reader's first unlabeled letter after this one in reading order."""
    return (
        items_for(reader)
        .filter(position__gt=item.position)
        .exclude(labels__reader=reader)
        .first()
    )


def progress(reader: LetterReviewReader) -> Tuple[int, int]:
    """(labeled, assigned) for one reader."""
    assigned = items_for(reader).count()
    labeled = LetterReviewLabel.objects.filter(
        reader=reader, item__readers=reader
    ).count()
    return labeled, assigned


@sensitive_variables()
def neighbours(
    reader: LetterReviewReader, item: LetterReviewItem
) -> Tuple[int, Optional[str], Optional[str]]:
    """This item's 1-based place in the reader's list, and the slugs either side."""
    slugs: List[str] = list(items_for(reader).values_list("slug", flat=True))
    index = slugs.index(item.slug)
    previous_slug = slugs[index - 1] if index > 0 else None
    next_slug = slugs[index + 1] if index + 1 < len(slugs) else None
    return index + 1, previous_slug, next_slug


@sensitive_variables()
def reader_letters(reader: LetterReviewReader) -> List[Dict[str, Any]]:
    """Each of the reader's letters in order, with their own mark or None.

    Only this reader's labels are read, so the list stays blind.
    """
    marks: Dict[int, str] = dict(
        LetterReviewLabel.objects.filter(reader=reader).values_list(
            "item_id", "verdict"
        )
    )
    names = dict(LETTER_REVIEW_VERDICTS)
    letters: List[Dict[str, Any]] = []
    for place, (item_id, slug) in enumerate(
        items_for(reader).values_list("id", "slug"), start=1
    ):
        verdict = marks.get(item_id)
        letters.append(
            {
                "place": place,
                "slug": slug,
                "verdict": verdict,
                "verdict_label": names[verdict] if verdict else None,
            }
        )
    return letters


def packet_finished(packet: LetterReviewPacket) -> bool:
    """True once every reader on the packet has labeled every letter of theirs."""
    return all(
        labeled >= assigned
        for labeled, assigned in (
            progress(reader)
            for reader in LetterReviewReader.objects.filter(packet=packet)
        )
    )


def export_open_to(user: Any, packet: LetterReviewPacket) -> bool:
    """Whether this user may download the packet's labels from the page.

    The export holds every reader's labels, so for a superuser who is also a
    reader on the packet it would show the other reader's marks on the
    letters they share. It stays shut to them until every reader is done.
    The letter_review_export command is not gated: it is run by hand.
    """
    if not getattr(user, "is_superuser", False):
        return False
    if not LetterReviewReader.objects.filter(packet=packet, user=user).exists():
        return True
    return packet_finished(packet)


def verdict_choices() -> Iterable[Dict[str, str]]:
    return [
        {
            "value": value,
            "label": label,
            "help": VERDICT_HELP[value],
            "shortcut": str(n),
        }
        for n, (value, label) in enumerate(LETTER_REVIEW_VERDICTS, start=1)
    ]
