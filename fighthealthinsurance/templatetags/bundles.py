"""``{% bundle "scrub" %}``: a dist bundle's URL that changes when its contents do.

The bundles have fixed names, so a CDN can keep serving an old copy after a
deploy. The content hash in the query string gives each build its own URL.
"""

import functools
import hashlib
from typing import Optional

from django import template
from django.conf import settings
from django.contrib.staticfiles import finders
from django.templatetags.static import static

register = template.Library()


def _path(name: str) -> str:
    return f"js/dist/{name}.bundle.js"


@functools.lru_cache(maxsize=None)
def _cached_version(name: str) -> Optional[str]:
    return _version(name)


def _version(name: str) -> Optional[str]:
    found = finders.find(_path(name))
    if not found or not isinstance(found, str):
        return None
    with open(found, "rb") as bundle:
        return hashlib.sha256(bundle.read()).hexdigest()[:12]


@register.simple_tag
def bundle(name: str) -> str:
    # A running pod's bundles never change; a dev server's do.
    version = _version(name) if settings.DEBUG else _cached_version(name)
    url = static(_path(name))
    return f"{url}?v={version}" if version else url
