"""Reusable, typed builders for schema.org structured data (JSON-LD).

This module centralizes construction of JSON-LD objects so pages stop
hand-rolling their own schema dicts and their own HTML escaping. Every builder
returns a plain ``dict`` (with the ``@context`` set) that can be embedded on a
page via :func:`render_json_ld` (or the ``{% json_ld %}`` template tag in
``structured_data_tags``).

Content-safety note: the Organization data describes this application's real
identity only. We deliberately do NOT emit awards, ``aggregateRating``,
``review``, or any statistic-style claims -- fabricated ratings are a policy
risk and none of these are verifiable here.
"""

import functools
import json
from typing import Any, Iterable, Mapping, Optional, Sequence, Union

from django.core.serializers.json import DjangoJSONEncoder
from django.templatetags.static import static
from django.utils.safestring import SafeString, mark_safe

SCHEMA_CONTEXT = "https://schema.org"

# Canonical public identity for Fight Health Insurance. This mirrors the
# canonical domain used by ``context_processors.canonical_url_context`` so all
# structured data points at the same origin regardless of the request host.
CANONICAL_DOMAIN = "https://www.fighthealthinsurance.com"
ORGANIZATION_NAME = "Fight Health Insurance"
ORGANIZATION_DESCRIPTION = (
    "Fight Health Insurance helps patients understand and appeal health "
    "insurance denials, including generating appeal letters."
)
# Logo lives in static; resolved to an absolute URL lazily (see _logo_url).
ORGANIZATION_LOGO_STATIC_PATH = "images/better-logo-optimized.png"

# Real, first-party profiles for the organization. These are the same links
# surfaced in the site footer, so ``sameAs`` stays truthful.
ORGANIZATION_SAME_AS: tuple[str, ...] = (
    "https://www.instagram.com/fighthealthinsurance/",
    "https://www.linkedin.com/company/fight-health-insurance",
    "https://www.youtube.com/@fighthealthinsuranceyt",
    "https://fighthealthinsurance.substack.com/",
    "https://www.reddit.com/r/fightpaperwork/",
    "https://www.tiktok.com/@fighthealthinsurancett",
    "https://github.com/orgs/fighthealthinsurance/repositories",
)

# Escaping identical in spirit to Django's ``json_script``: these three
# characters are the only ones that can let JSON break out of a
# ``<script>`` element, and the line/paragraph separators are escaped so the
# payload is also valid JavaScript. See render_json_ld.
_JSON_SCRIPT_ESCAPES = {
    ord(">"): "\\u003e",
    ord("<"): "\\u003c",
    ord("&"): "\\u0026",
    ord("\u2028"): "\\u2028",
    ord("\u2029"): "\\u2029",
}


@functools.lru_cache(maxsize=1)
def _logo_url() -> str:
    """Absolute URL to the organization logo.

    ``static()`` yields a root-relative path (e.g. ``/static/images/...``); we
    prefix the canonical domain so the value is an absolute URL as schema.org
    expects for an ``ImageObject``/logo.
    """
    path = static(ORGANIZATION_LOGO_STATIC_PATH)
    if path.startswith(("http://", "https://")):
        return path
    return f"{CANONICAL_DOMAIN}{path}"


def render_json_ld(data: Union[Mapping[str, Any], Sequence[Any]]) -> SafeString:
    """Render ``data`` as a hardened ``<script type="application/ld+json">`` tag.

    This is the ONE sanctioned way to embed JSON-LD. ``data`` may be a single
    node object (a mapping) or a list of node objects -- both are valid JSON-LD
    documents. It serializes with Django's JSON encoder and then escapes ``<``,
    ``>``, ``&`` (plus the JS line separators) so a string value such as
    ``"</script>"`` cannot terminate the script element or inject markup.
    Callers must NOT wrap the result in ``|safe`` after their own string
    munging.
    """
    payload = json.dumps(data, cls=DjangoJSONEncoder, ensure_ascii=False)
    payload = payload.translate(_JSON_SCRIPT_ESCAPES)
    return mark_safe(f'<script type="application/ld+json">{payload}</script>')


def organization(
    name: str = ORGANIZATION_NAME,
    url: str = CANONICAL_DOMAIN,
    logo: Optional[str] = None,
    same_as: Optional[Sequence[str]] = ORGANIZATION_SAME_AS,
    description: Optional[str] = ORGANIZATION_DESCRIPTION,
) -> dict[str, Any]:
    """Build a schema.org ``Organization`` object for the site.

    Only truthful, first-party identity fields are emitted (name, url, logo,
    sameAs, description). No ratings/awards/statistics are ever added.
    """
    data: dict[str, Any] = {
        "@context": SCHEMA_CONTEXT,
        "@type": "Organization",
        "name": name,
        "url": url,
        "logo": logo if logo is not None else _logo_url(),
    }
    if description:
        data["description"] = description
    if same_as:
        data["sameAs"] = list(same_as)
    return data


def website(
    name: str = ORGANIZATION_NAME,
    url: str = CANONICAL_DOMAIN,
    search_url_template: Optional[str] = None,
) -> dict[str, Any]:
    """Build a schema.org ``WebSite`` object.

    When ``search_url_template`` is supplied it must contain the
    ``{search_term_string}`` placeholder; a ``SearchAction`` (sitelinks search
    box) is then attached. If omitted (this site has no search endpoint), no
    ``potentialAction`` is emitted.
    """
    data: dict[str, Any] = {
        "@context": SCHEMA_CONTEXT,
        "@type": "WebSite",
        "name": name,
        "url": url,
    }
    if search_url_template:
        data["potentialAction"] = {
            "@type": "SearchAction",
            "target": {
                "@type": "EntryPoint",
                "urlTemplate": search_url_template,
            },
            "query-input": "required name=search_term_string",
        }
    return data


# A breadcrumb item may be given as (name, url) or as a mapping with "name"
# and (optionally) "url"/"item" keys.
BreadcrumbItem = Union[tuple[str, str], tuple[str, None], Mapping[str, Any]]


def breadcrumb_list(items: Iterable[BreadcrumbItem]) -> dict[str, Any]:
    """Build a schema.org ``BreadcrumbList`` from an ordered list of crumbs.

    Each crumb may be a ``(name, url)`` tuple or a mapping with ``name`` and an
    optional ``url``/``item``. Positions are 1-indexed in the given order.
    """
    elements: list[dict[str, Any]] = []
    for position, item in enumerate(items, start=1):
        if isinstance(item, Mapping):
            name = item["name"]
            url = item.get("item", item.get("url"))
        else:
            name, url = item
        element: dict[str, Any] = {
            "@type": "ListItem",
            "position": position,
            "name": name,
        }
        if url:
            element["item"] = url
        elements.append(element)
    return {
        "@context": SCHEMA_CONTEXT,
        "@type": "BreadcrumbList",
        "itemListElement": elements,
    }


# A FAQ entry is a (question, answer) pair or a mapping with those keys.
FaqItem = Union[tuple[str, str], Mapping[str, str]]


def faq_page(items: Iterable[FaqItem]) -> dict[str, Any]:
    """Build a schema.org ``FAQPage`` from question/answer pairs.

    Each entry is a ``(question, answer)`` tuple or a mapping with ``question``
    and ``answer`` keys. Answer text is kept verbatim; the render helper handles
    escaping.
    """
    entities: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, Mapping):
            question = item["question"]
            answer = item["answer"]
        else:
            question, answer = item
        entities.append(
            {
                "@type": "Question",
                "name": question,
                "acceptedAnswer": {
                    "@type": "Answer",
                    "text": answer,
                },
            }
        )
    return {
        "@context": SCHEMA_CONTEXT,
        "@type": "FAQPage",
        "mainEntity": entities,
    }


# A HowTo step may be plain text, or a mapping with "name"/"text"/"url".
HowToStepItem = Union[str, Mapping[str, Any]]


def how_to(
    name: str,
    steps: Iterable[HowToStepItem],
    description: Optional[str] = None,
    total_time: Optional[str] = None,
) -> dict[str, Any]:
    """Build a schema.org ``HowTo`` object.

    ``steps`` may be plain strings (used as the step text) or mappings with
    ``name``/``text``/``url``. ``total_time`` should be an ISO-8601 duration
    (e.g. ``"PT15M"``) when provided.
    """
    step_elements: list[dict[str, Any]] = []
    for position, step in enumerate(steps, start=1):
        element: dict[str, Any] = {
            "@type": "HowToStep",
            "position": position,
        }
        if isinstance(step, Mapping):
            if step.get("name"):
                element["name"] = step["name"]
            element["text"] = step.get("text", step.get("name", ""))
            if step.get("url"):
                element["url"] = step["url"]
        else:
            element["text"] = step
        step_elements.append(element)
    data: dict[str, Any] = {
        "@context": SCHEMA_CONTEXT,
        "@type": "HowTo",
        "name": name,
        "step": step_elements,
    }
    if description:
        data["description"] = description
    if total_time:
        data["totalTime"] = total_time
    return data


def _web_page(
    page_type: str,
    name: str,
    url: Optional[str] = None,
    description: Optional[str] = None,
    breadcrumb: Optional[Mapping[str, Any]] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Shared builder for ``WebPage``-family objects."""
    data: dict[str, Any] = {
        "@context": SCHEMA_CONTEXT,
        "@type": page_type,
        "name": name,
    }
    if url:
        data["url"] = url
    if description:
        data["description"] = description
    if breadcrumb:
        data["breadcrumb"] = dict(breadcrumb)
    if extra:
        data.update(extra)
    return data


def web_page(
    name: str,
    url: Optional[str] = None,
    description: Optional[str] = None,
    breadcrumb: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Build a schema.org ``WebPage`` object."""
    return _web_page(
        "WebPage",
        name=name,
        url=url,
        description=description,
        breadcrumb=breadcrumb,
    )


def medical_web_page(
    name: str,
    url: Optional[str] = None,
    description: Optional[str] = None,
    breadcrumb: Optional[Mapping[str, Any]] = None,
    about: Optional[str] = None,
    specialty: Optional[str] = None,
) -> dict[str, Any]:
    """Build a schema.org ``MedicalWebPage`` object.

    ``about`` names the primary subject (as a ``Thing`` name) and ``specialty``
    maps to ``MedicalWebPage.specialty`` when provided.
    """
    extra: dict[str, Any] = {}
    if about:
        extra["about"] = {"@type": "Thing", "name": about}
    if specialty:
        extra["specialty"] = specialty
    return _web_page(
        "MedicalWebPage",
        name=name,
        url=url,
        description=description,
        breadcrumb=breadcrumb,
        # Normalize an empty dict to None so _web_page's `if extra:` skips it.
        extra=extra or None,
    )
