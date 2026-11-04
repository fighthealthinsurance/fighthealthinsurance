"""BokehJS script tags matching the installed Python bokeh.

``components()`` output only renders under the same BokehJS version that
produced it; a hardcoded CDN version drifts whenever bokeh is upgraded and
every chart page goes blank with a "version mismatch" console error.
"""

from bokeh.resources import CDN
from django import template
from django.utils.safestring import mark_safe

register = template.Library()


@register.simple_tag
def bokeh_js() -> str:
    return mark_safe(CDN.render_js())
