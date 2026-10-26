"""``{% bundle "scrub" %}`` and ``{% versioned "js/pwyw.js" %}``: static URLs
that change when the file's contents do.

Static files have fixed names, so a CDN or browser can keep serving an old
copy after a deploy, beside new pages that expect the new one. The content
hash in the query string gives each build its own URL.
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
def _cached_version(path: str) -> Optional[str]:
    return _version(path)


def _version(path: str) -> Optional[str]:
    found = finders.find(path)
    if not found or not isinstance(found, str):
        return None
    try:
        with open(found, "rb") as static_file:
            return hashlib.sha256(static_file.read()).hexdigest()[:12]
    except OSError:
        return None


@register.simple_tag
def versioned(path: str) -> str:
    # A running pod's static files never change; a dev server's do.
    version = _version(path) if settings.DEBUG else _cached_version(path)
    url = static(path)
    return f"{url}?v={version}" if version else url


@register.simple_tag
def bundle(name: str) -> str:
    return versioned(_path(name))
