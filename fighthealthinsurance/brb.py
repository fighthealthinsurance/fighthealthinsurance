"""The "be right back" page, and the BRB flag that puts it up.

While the site is down on purpose, the landing page tells a short story of
why, with a 503, and every other page keeps working: people already in the
flow finish their appeal, and new people are asked to come back later (the
"drain" the flag has always done; ``views.root_view`` picks the page). The
story is named by the BRB environment variable, read into ``settings.BRB``
at startup, so changing it is a rollout of the web deployment with the new
value (k8s/deploy.yaml):

    BRB=LIVE        the site as usual (also when the variable is unset)
    BRB=EL_NINO     most of the models run on our own servers on the West
                    Coast, and the weather took them out
    BRB=MIGRATION   the servers are in the back of the car on the way to
                    their new home
    BRB=BRB         the original page, from the weekend PBS sent everyone

A value that names no story still takes the site down, since setting it is
how the site is taken down, and shows the original page with a warning in
the log: a typo under pressure fails toward "down", not toward a landing
page that promises letters the models cannot write.

People already in the flow never see this page, so pair the flag with a
SiteBanner (constants.SITE_BANNER_CANNED_MESSAGES has a message for each
story) so they hear it too.

When the web tier itself is going away (a migration, say), the same page
is served as a static site instead: ``manage.py render_brb_static`` renders
every story into the static-brb repository, and Cloudflare is pointed there
for the duration (that repository's README has the steps).

To add a story: a template under templates/brb/ with the page's body (an
<h1>, a few paragraphs and a figure; see the ones there), and a BrbReason
for it below. The page is standalone on purpose, loading no stylesheet or
script, so it renders when nothing else does, and it says no-cache and
noindex in its own <head>, so the static copy says the same from any host.
"""

import functools
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

from django.conf import settings
from loguru import logger

# The value, and the default, that keeps the site up.
LIVE = "LIVE"

# Sent with the 503 so crawlers and well-behaved clients know when to look
# again. An hour: these outages are measured in hours, not minutes.
RETRY_AFTER_SECONDS = 60 * 60


@dataclass(frozen=True)
class BrbReason:
    """One story the page can tell: the BRB value that picks it, the tab
    title, and the template (under templates/brb/) with the page's body."""

    key: str
    title: str
    template: str

    @property
    def slug(self) -> str:
        """The story's file name in the static site: EL_NINO is el-nino."""
        return self.key.lower().replace("_", "-")


BRB_REASONS = {
    reason.key: reason
    for reason in (
        BrbReason("BRB", "We’ll Be Right Back", "brb/classic.html"),
        BrbReason("EL_NINO", "Back After the Weather", "brb/el_nino.html"),
        BrbReason("MIGRATION", "Moving Day", "brb/migration.html"),
    )
}


def squash(value: str) -> str:
    """A value with only its letters and digits left, in upper case, so
    "el niño", " El-Nino ", "elnino" and "EL_NINO" all name EL_NINO and a
    value typed under pressure still lands."""
    ascii_only = unicodedata.normalize("NFKD", value).encode("ascii", "ignore")
    return re.sub(r"[^A-Z0-9]+", "", ascii_only.decode().upper())


_REASONS_BY_SQUASHED_KEY = {squash(key): reason for key, reason in BRB_REASONS.items()}


def reason_named(value: str) -> Optional[BrbReason]:
    """The story a value names, spelling forgiven, or None for no story."""
    return _REASONS_BY_SQUASHED_KEY.get(squash(value))


@functools.lru_cache(maxsize=16)
def _reason_for(raw: str) -> Optional[BrbReason]:
    key = squash(raw)
    if key in ("", LIVE):
        return None
    reason = reason_named(raw)
    if reason is None:
        # Cached per value, so this is said once per process, not per request.
        logger.warning(
            f"BRB={raw!r} names no story (one of {', '.join(BRB_REASONS)}): "
            "the site is down with the original page"
        )
        return BRB_REASONS["BRB"]
    return reason


def brb_reason() -> Optional[BrbReason]:
    """The story the landing page tells while the site is down, or None
    while it is up. Read on every request, so a test can override
    ``settings.BRB``."""
    return _reason_for(str(getattr(settings, "BRB", None) or ""))
