"""One safe way to put JSON-LD on a page.

Only the renderer lives here. The site's own Organization and WebSite nodes
are already emitted for every page by ``partials/jsonld_organization.html``,
included from ``base.html``, and that version carries stable ``@id`` anchors,
both founders and a publisher link, so nothing here duplicates it.

Taken from #903, trimmed to the part the glossary needs.
"""

import json
from typing import Any, Mapping, Sequence, Union

from django.core.serializers.json import DjangoJSONEncoder
from django.utils.safestring import SafeString, mark_safe

# The characters that can let JSON break out of a <script> element, plus the
# line and paragraph separators, so the payload is valid JavaScript too.
_JSON_SCRIPT_ESCAPES = {
    ord(">"): "\\u003e",
    ord("<"): "\\u003c",
    ord("&"): "\\u0026",
    ord("\u2028"): "\\u2028",
    ord("\u2029"): "\\u2029",
}


def render_json_ld(
    data: Union[Mapping[str, Any], Sequence[Mapping[str, Any]]],
) -> SafeString:
    """Render ``data`` as a hardened ``<script type="application/ld+json">``.

    This is the one sanctioned way to embed JSON-LD. ``data`` may be a single
    node object or a list of them, both valid JSON-LD. It serializes with
    Django's JSON encoder and then escapes ``<``, ``>`` and ``&``, so a string
    value such as ``"</script>"`` cannot end the element or inject markup.

    Callers must not wrap the result in ``|safe`` after munging it themselves.
    """
    payload = json.dumps(data, cls=DjangoJSONEncoder, ensure_ascii=False)
    payload = payload.translate(_JSON_SCRIPT_ESCAPES)
    return mark_safe(f'<script type="application/ld+json">{payload}</script>')
