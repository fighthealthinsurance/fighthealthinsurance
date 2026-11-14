from typing import Any

from django import template
from django.template.base import Parser, Token
from django.utils.safestring import mark_safe

register = template.Library()


class CaptureNode(template.Node):
    def __init__(self, nodelist: template.NodeList, var_name: str) -> None:
        self.nodelist = nodelist
        self.var_name = var_name

    def render(self, context: Any) -> str:
        # Already rendered (and escaped) by the template engine.
        context[self.var_name] = mark_safe(self.nodelist.render(context).strip())
        return ""


@register.tag
def capture(parser: Parser, token: Token) -> CaptureNode:
    """``{% capture as name %}...{% endcapture %}``: render once, reuse the text.

    Lets base.html use a page's title block in <title> and the share tags.
    """
    bits = token.split_contents()
    if len(bits) != 3 or bits[1] != "as":
        raise template.TemplateSyntaxError("Usage: {% capture as name %}")
    nodelist = parser.parse(("endcapture",))
    parser.delete_first_token()
    return CaptureNode(nodelist, bits[2])
