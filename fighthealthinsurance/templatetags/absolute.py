from typing import Any

from django import template
from django.http import HttpRequest
from django.urls import reverse

register = template.Library()


@register.simple_tag(takes_context=True)
def absolute_url(
    context: dict[str, Any], view_name: str, *args: Any, **kwargs: Any
) -> str:
    request: HttpRequest = context["request"]
    result: str = request.build_absolute_uri(
        reverse(view_name, args=args, kwargs=kwargs)
    )
    return result
