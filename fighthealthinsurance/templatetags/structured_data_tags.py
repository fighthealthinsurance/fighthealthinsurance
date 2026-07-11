"""Template tags for embedding schema.org JSON-LD safely.

These wrap :mod:`fighthealthinsurance.structured_data` so templates get a single
hardened way to emit JSON-LD (``render_json_ld`` escapes ``<``/``>``/``&``),
rather than hand-rolling ``<script>`` blocks with ``|safe``.
"""

from typing import Any, Mapping

from django import template
from django.utils.safestring import SafeString, mark_safe

from fighthealthinsurance import structured_data

register = template.Library()


@register.simple_tag
def json_ld(data: Mapping[str, Any]) -> SafeString:
    """Render a JSON-LD dict (from a ``structured_data`` builder) as a script tag.

    Usage::

        {% load structured_data_tags %}
        {% json_ld my_breadcrumb_dict %}
    """
    return structured_data.render_json_ld(data)


@register.simple_tag
def sitewide_structured_data() -> SafeString:
    """Render the sitewide Organization + WebSite JSON-LD.

    Intended to be dropped once into the base template so every page carries the
    organization identity and website node. Kept self-contained (no template
    context needed) to minimize churn in ``base.html``.
    """
    org = structured_data.render_json_ld(structured_data.organization())
    site = structured_data.render_json_ld(structured_data.website())
    return mark_safe(f"{org}\n{site}")
