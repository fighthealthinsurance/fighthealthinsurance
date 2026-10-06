"""The intake page's agreement boxes, worded from consent.py so the page and
the record of what was ticked can't drift apart."""

from django import template
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import SafeString

register = template.Library()


@register.simple_tag
def consent_label(name: str) -> SafeString:
    if name == "pii":
        return format_html("I've taken my personal details out of the letter above.")
    if name == "privacy":
        return format_html(
            'I have read and understand the <a class="link" href="{}">privacy policy.</a>',
            reverse("privacy_policy"),
        )
    if name == "tos":
        return format_html(
            'I agree to the <a href="{}">terms of service</a>. I\'ll use this site '
            "only for my own insurance appeals, or for someone I'm helping who asked "
            "me to, not to diagnose or treat any condition.",
            reverse("tos"),
        )
    if name == "personalonly":
        return format_html(
            "This is for <b>my own appeal</b> or for someone I'm helping who asked "
            'me to. (Doctors, therapists and offices: see our <a href="{}">professional '
            "version</a>.)",
            reverse("pro_version"),
        )
    raise template.TemplateSyntaxError(f"No agreement box named {name!r}")
