"""Render the "be right back" page as a static site, for static-brb.

Usage, from the repository root with the static-brb checkout beside it::

    python manage.py render_brb_static --out ../static-brb --story EL_NINO

Writes, under --out: index.html and 404.html, both telling the story named
(the 404 copy is what GitHub Pages and Cloudflare Pages serve for every
other path, so a person who arrives at /scan sees the page too); one
<story>.html per story (el-nino.html, migration.html, brb.html), to preview
the others; and images/, with every picture the pages name, copied from the
app's static files. Nothing else under --out is touched, so the host's own
files (README.md, _headers, functions/) stay as they are.

The pages are the app's own template (templates/brb.html, brb.py) rendered
without a request, with the pictures at a relative images/ path, so the
static site says exactly what the app's 503 page says: no-cache and noindex
in its <head>, and no stylesheet or script to fetch. Commit and push the
result, then point Cloudflare at the site (static-brb's README has the
steps).
"""

import re
import shutil
from pathlib import Path
from typing import Set, Tuple

from django.contrib.staticfiles import finders
from django.core.management.base import BaseCommand, CommandError
from django.template.loader import render_to_string
from django.templatetags.static import static

from fighthealthinsurance import brb

# Where the pages find their pictures in the static site.
IMAGES = "images/"


def render_story(reason: brb.BrbReason) -> Tuple[str, Set[str]]:
    """The story's page, with its pictures under images/, and their names."""
    rendered = str(render_to_string("brb.html", {"reason": reason}))
    # {% static 'images/x.jpg' %} rendered to wherever the app serves its
    # static files from; the static site keeps them beside the pages.
    html = rendered.replace(f'src="{static(IMAGES)}', f'src="{IMAGES}')
    pictures = set(re.findall(rf'src="{IMAGES}([^"?]+)', html))
    return html, pictures


class Command(BaseCommand):
    help = (
        "Render the be-right-back page as a static site, for the static-brb "
        "repository: index.html for the story named, one page per story, "
        "and the pictures they use."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--out",
            required=True,
            help="The directory to write into (the static-brb checkout).",
        )
        parser.add_argument(
            "--story",
            default="EL_NINO",
            help=(
                "The story index.html tells, one of "
                f"{', '.join(brb.BRB_REASONS)} (default EL_NINO)."
            ),
        )

    def handle(self, *args, **options):
        story = brb.reason_named(options["story"])
        if story is None:
            raise CommandError(
                f"{options['story']!r} names no story; one of "
                f"{', '.join(brb.BRB_REASONS)}"
            )
        out = Path(options["out"])
        (out / IMAGES).mkdir(parents=True, exist_ok=True)

        pictures: Set[str] = set()
        for reason in brb.BRB_REASONS.values():
            html, named = render_story(reason)
            pictures |= named
            (out / f"{reason.slug}.html").write_text(html, encoding="utf-8")
            if reason is story:
                for name in ("index.html", "404.html"):
                    (out / name).write_text(html, encoding="utf-8")

        for picture in sorted(pictures):
            source = finders.find(f"{IMAGES}{picture}")
            if source is None:
                raise CommandError(f"{IMAGES}{picture} is not among the static files")
            shutil.copyfile(source, out / IMAGES / picture)

        self.stdout.write(
            f"Wrote {story.key} as index.html and 404.html, "
            f"{len(brb.BRB_REASONS)} story pages and {len(pictures)} pictures "
            f"to {out}"
        )
