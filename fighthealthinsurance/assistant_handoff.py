"""What an AI assistant sends to fill in the appeal form, held for the person
to open once.

The MCP server's ``prepare_appeal`` tool calls :func:`create_handoff` with the
denial letter's text (or a summary) that the person chose to share, and gives
the assistant a link with a code after its ``#``. The person opens the link,
presses one button, and :func:`claim_handoff` hands the text back to fill in
the usual appeal form (assistant_handoff_views.py). No Denial exists until the
person submits that form.

How the text is kept:

- **Sealed with a key made from the link's code.** The code is never stored.
  The row holds a SHA-256 digest of it to find the row by, and the Fernet
  ciphertext. The digest and the key come from the code through two
  separately labelled derivations, so the stored digest cannot be turned into
  the key, and the database and its backups hold only ciphertext.
- **Opens for at most HANDOFF_TTL (2 hours),** enforced two ways: the lookup
  only finds rows that have not expired, and Fernet refuses ciphertext older
  than the TTL. A row nobody opens is deleted after it expires, by the next
  ``create_handoff`` call or the ``sweep_assistant_handoffs`` command (every
  10 minutes, whatever the flags say), so it can outlive the TTL by about
  that long, longer if the CronJob stops. Database backups taken while a row
  exists keep its ciphertext for their own retention, which nobody can open
  without the link.
- **Opened once.** The row is read and deleted in one transaction, and a
  delete that does not remove exactly one row means another request got there
  first. Decryption happens after the delete.

Nothing here logs the text or the code. Outcomes are counted (the Prometheus
counters below), never described.
"""

import base64
import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Iterator, Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from loguru import logger
from prometheus_client import REGISTRY, Counter
from prometheus_client.core import GaugeMetricFamily, Metric
from prometheus_client.registry import Collector

from fighthealthinsurance.models import AssistantHandoff

HANDOFF_TTL = timedelta(hours=2)
# secrets.token_urlsafe(32): 256 bits, 43 URL-safe characters.
CODE_BYTES = 32
CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
# The two derivations from the code. Different labels, so neither output can
# be turned into the other.
_LOOKUP_LABEL = b"fhi-assistant-handoff-lookup-v1\x00"
_KEY_LABEL = b"fhi-assistant-handoff-key-v1"
# A bound link is resealed under the code and the browser's binder together.
_BOUND_KEY_LABEL = b"fhi-assistant-handoff-key-v2-bound"
_BINDER_LABEL = b"fhi-assistant-handoff-binder-v2\x00"
PAYLOAD_VERSION_V1 = 1
PAYLOAD_VERSION_V2 = 2
ACCEPTED_PAYLOAD_VERSIONS = (PAYLOAD_VERSION_V1, PAYLOAD_VERSION_V2)
KINDS = ("site", "chat")
BINDER_BYTES = 32
CLIENT_LABEL_MAX = 40
# Letters, digits and the punctuation a product name and version use, so a
# User-Agent like "openai-mcp/1.0.0 (Codex)" keeps its shape.
_CLIENT_LABEL_OK = re.compile(r"[^A-Za-z0-9 ._()/-]")
# Rows made in this window and still in the table count toward
# MCP_PREPARE_APPEAL_MAX_PER_MINUTE. Opening a link deletes its row, so this
# caps links made and not yet opened in the last minute, not every link made:
# the cap is there to keep a burst from filling the live slots, and an opened
# link holds none.
RATE_WINDOW = timedelta(seconds=60)

LINKS_MADE = Counter(
    "fhi_assistant_handoff_links_made_total",
    "Links prepare_appeal made",
)
FORMS_OPENED = Counter(
    "fhi_assistant_handoff_forms_opened_total",
    "Assistant handoff links that opened the filled-in form",
)
DEAD_OPENS = Counter(
    "fhi_assistant_handoff_dead_opens_total",
    "Assistant handoff links that were already used, expired or never existed",
)
REFUSED_AT_CAP = Counter(
    "fhi_assistant_handoff_refused_at_cap_total",
    "prepare_appeal calls refused at the live or per-minute cap",
)
# Counted where the delete happens. prepare_appeal's own sweep runs in a web
# pod, which Prometheus scrapes; the 10-minute CronJob is its own short-lived
# process that nothing scrapes, so the rows it deletes show up in its log line
# and not here. Read this as "at least", and as close to exact while
# prepare_appeal is busy, which is when a burst would be.
LINKS_EXPIRED = Counter(
    "fhi_assistant_handoff_links_expired_total",
    "Links nobody opened, deleted after expiry by prepare_appeal's sweep",
)


class HandoffCapacityError(Exception):
    """Too many live links, or too many made in the last minute and not yet
    opened."""


@dataclass(frozen=True)
class Handoff:
    """A new link's code, which only the link carries, and its expiry."""

    code: str
    expires_at: datetime


@dataclass(frozen=True)
class HandoffContent:
    """What the assistant sent, once the person opens the link."""

    letter: str
    procedure: str
    condition: str
    kind: str = "site"
    client: str = ""
    # The AssistantDraft a chat link was made for (its primary key), or None.
    draft: Optional[int] = None


def is_code(value: object) -> bool:
    return isinstance(value, str) and CODE_PATTERN.match(value) is not None


def _lookup(code: str) -> str:
    """The digest the row is found by."""
    return hashlib.sha256(_LOOKUP_LABEL + code.encode("ascii")).hexdigest()


def _fernet(code: str) -> Fernet:
    """The key the row is sealed with, which only the code can make."""
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_KEY_LABEL).derive(
        code.encode("ascii")
    )
    return Fernet(base64.urlsafe_b64encode(key))


def _bound_fernet(code: str, binder: str) -> Fernet:
    """The key a bound row is sealed with: the code and the browser's binder."""
    key = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=_BOUND_KEY_LABEL
    ).derive(code.encode("ascii") + b"\x00" + binder.encode("ascii"))
    return Fernet(base64.urlsafe_b64encode(key))


def _binder_digest(binder: str) -> str:
    return hashlib.sha256(_BINDER_LABEL + binder.encode("ascii")).hexdigest()


def new_binder() -> str:
    """A browser's secret for binding links, kept in its session."""
    return secrets.token_urlsafe(BINDER_BYTES)


def v2_enabled() -> bool:
    return bool(getattr(settings, "MCP_HANDOFF_V2_ENABLED", False))


def client_label(name: object) -> str:
    """A short, plain label for the client that made a link, or ""."""
    if not isinstance(name, str):
        return ""
    return _CLIENT_LABEL_OK.sub("", name).strip()[:CLIENT_LABEL_MAX]


def sweep_expired(now: Optional[datetime] = None) -> int:
    """Delete every row past its expiry. Returns how many went."""
    deleted, _ = AssistantHandoff.objects.filter(
        expires_at__lte=now or timezone.now()
    ).delete()
    if deleted:
        LINKS_EXPIRED.inc(deleted)
    return deleted


def live_count() -> int:
    return AssistantHandoff.objects.filter(expires_at__gt=timezone.now()).count()


def create_handoff(
    letter: str,
    procedure: str = "",
    condition: str = "",
    kind: str = "site",
    client: str = "",
    draft: Optional[int] = None,
) -> Handoff:
    """Seal what the assistant sent and return the new link's code.

    Raises HandoffCapacityError at either cap. The arguments are already
    cleaned and capped by the caller (mcp_server.prepare_appeal). ``kind``
    and ``client`` are kept only in a v2 payload (MCP_HANDOFF_V2_ENABLED),
    as is ``draft``, the AssistantDraft a chat link belongs to.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown handoff kind {kind!r}")
    now = timezone.now()
    sweep_expired(now)
    live = AssistantHandoff.objects.filter(expires_at__gt=now).count()
    recent = AssistantHandoff.objects.filter(created_at__gt=now - RATE_WINDOW).count()
    if (
        live >= settings.MCP_PREPARE_APPEAL_MAX_LIVE
        or recent >= settings.MCP_PREPARE_APPEAL_MAX_PER_MINUTE
    ):
        REFUSED_AT_CAP.inc()
        raise HandoffCapacityError()
    code = secrets.token_urlsafe(CODE_BYTES)
    # ensure_ascii=False: the default escapes every non-ASCII character to
    # six bytes (twelve for an emoji), which made a 20,000-character letter
    # in Cyrillic about 160 KB sealed (54 KB as UTF-8). The caller strips
    # lone surrogates, which UTF-8 can't encode.
    fields: dict[str, object] = {
        "v": PAYLOAD_VERSION_V1,
        "letter": letter,
        "procedure": procedure,
        "condition": condition,
    }
    if v2_enabled():
        fields.update(v=PAYLOAD_VERSION_V2, kind=kind, client=client_label(client))
        if draft is not None:
            fields["draft"] = int(draft)
    payload = json.dumps(fields, ensure_ascii=False).encode("utf-8")
    expires_at = now + HANDOFF_TTL
    AssistantHandoff.objects.create(
        lookup=_lookup(code),
        sealed=_fernet(code).encrypt(payload),
        expires_at=expires_at,
    )
    LINKS_MADE.inc()
    return Handoff(code=code, expires_at=expires_at)


def _live_row(lookup: str) -> Optional[AssistantHandoff]:
    """The unexpired row for a lookup digest, locked until the transaction
    ends (a no-op on sqlite, whose writes are serialised anyway)."""
    return (
        AssistantHandoff.objects.select_for_update()
        .filter(lookup=lookup, expires_at__gt=timezone.now())
        .only("pk", "sealed", "bound")
        .first()
    )


def _unseal(sealed: bytes, fernet: Fernet) -> Optional[bytes]:
    try:
        return fernet.decrypt(sealed, ttl=int(HANDOFF_TTL.total_seconds()))
    except (InvalidToken, ValueError):
        return None


def _content(plain: bytes) -> Optional[HandoffContent]:
    try:
        payload = json.loads(plain)
    except ValueError:
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("v") not in ACCEPTED_PAYLOAD_VERSIONS
    ):
        return None
    letter = payload.get("letter")
    if not isinstance(letter, str) or not letter:
        return None
    procedure = payload.get("procedure")
    condition = payload.get("condition")
    kind = payload.get("kind")
    draft = payload.get("draft")
    return HandoffContent(
        letter=letter,
        procedure=procedure if isinstance(procedure, str) else "",
        condition=condition if isinstance(condition, str) else "",
        kind=kind if kind in KINDS else "site",
        client=client_label(payload.get("client")),
        draft=draft if isinstance(draft, int) and not isinstance(draft, bool) else None,
    )


def claim_handoff(
    code: str, binder: Optional[str] = None, consume: bool = True
) -> Optional[HandoffContent]:
    """Open a link and return what it held, or None when the code is
    malformed, unknown, expired, already used, or bound to another browser.

    ``binder`` is the opening browser's secret (new_binder, kept in its
    session). The first open with a binder reseals the row under the code
    and the binder together and records the binder's digest, so from then on
    only that browser can open it. ``consume`` deletes the row (the one-use
    rule); with ``consume=False`` the bound row stays until it is consumed
    or expires, for a flow that reads it more than once.
    """
    if not is_code(code):
        DEAD_OPENS.inc()
        return None
    with transaction.atomic():
        row = _live_row(_lookup(code))
        if row is None:
            DEAD_OPENS.inc()
            return None
        sealed = bytes(row.sealed)
        if row.bound:
            if binder is None or _binder_digest(binder) != row.bound:
                DEAD_OPENS.inc()
                return None
            plain = _unseal(sealed, _bound_fernet(code, binder))
        else:
            plain = _unseal(sealed, _fernet(code))
        if plain is None:
            DEAD_OPENS.inc()
            return None
        if consume:
            # Exactly one row deleted, or another request opened it first.
            deleted, _ = AssistantHandoff.objects.filter(pk=row.pk).delete()
            if deleted != 1:
                DEAD_OPENS.inc()
                return None
        elif binder is not None and not row.bound:
            # The first open binds: resealed so the code alone no longer opens it.
            updated = AssistantHandoff.objects.filter(pk=row.pk, bound="").update(
                sealed=_bound_fernet(code, binder).encrypt(plain),
                bound=_binder_digest(binder),
            )
            if updated != 1:
                DEAD_OPENS.inc()
                return None
    content = _content(plain)
    if content is None:
        DEAD_OPENS.inc()
        return None
    if consume:
        FORMS_OPENED.inc()
    return content


# ---------------------------------------------------------------------------
# A scrape-time gauge of live links, like intake_outbox_metrics.py
#
# Read from the table at scrape time rather than set when a link is made or
# opened: each web pod keeps its own registry, a link made on one pod is
# often opened on another, and the sweep CronJob is a process nothing
# scrapes, so a gauge each pod kept for itself would be wrong on every pod.
# The count is the same one create_handoff checks the cap against, on a
# table the caps hold to a few hundred rows; every pod reports the same
# number, so alerts take max(), not sum() (k8s/assistant-handoff-alerts.yaml).
# ---------------------------------------------------------------------------

_LIVE = (
    "fhi_assistant_handoff_live_links",
    "Assistant handoff links not yet opened or expired",
)


class AssistantHandoffCollector(Collector):
    def describe(self) -> Iterable[Metric]:
        yield GaugeMetricFamily(*_LIVE)

    def collect(self) -> Iterator[Metric]:
        live = GaugeMetricFamily(*_LIVE)
        try:
            live.add_metric([], live_count())
        except Exception:
            # Table not migrated yet, or the database away: an empty scrape
            # and a log line, never a 500 on /metrics.
            logger.warning("assistant handoff metrics unavailable")
        yield live


_registered = False


def register_assistant_handoff_collector() -> None:
    global _registered
    if _registered:
        return
    try:
        REGISTRY.register(AssistantHandoffCollector())
    except ValueError:
        pass  # already registered (the app registry loaded twice in tests)
    _registered = True
