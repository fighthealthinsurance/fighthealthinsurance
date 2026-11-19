"""/evil/robot-id: where the X-Warning header sends would-be pen testers.

Every response without the X-Evil secret carries an X-Warning header
(PenTestWarningMiddleware) asking the client to request this path if it is
attempting to be evil. A request here grants nothing: it is logged, and the
support alias is emailed so a person can look at who asked.

Every request is logged. The email is throttled, since anyone can hit this
path as often as they like and the mailbox should not fill up when they do.
"""

from django.conf import settings
from django.core.cache import cache
from django.core.mail import send_mail
from django.http import HttpRequest, HttpResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt

from loguru import logger

from fighthealthinsurance.client_network import CF_IP_META
from fighthealthinsurance.constants import FHI_SUPPORT_EMAIL

EVIL_ROBOT_ID_PATH = "/evil/robot-id"

# At most one email per window; the log line is written every time.
EMAIL_THROTTLE_SECONDS = 15 * 60
_EMAIL_THROTTLE_KEY = "evil-robot-id-email-sent"

# What the requester chose to tell us about itself, cut to a sane length:
# all of it is written by the client.
_MAX_VALUE_CHARS = 300
_REPORTED_META = (
    ("CF-Connecting-IP", CF_IP_META),
    ("X-Forwarded-For", "HTTP_X_FORWARDED_FOR"),
    ("Remote address", "REMOTE_ADDR"),
    ("User-Agent", "HTTP_USER_AGENT"),
    ("Referer", "HTTP_REFERER"),
    ("Host", "HTTP_HOST"),
)

RESPONSE_TEXT = (
    "Thank you for identifying yourself. Your request has been logged and a "
    "human has been notified. You do NOT have permission to test this site "
    f"unless someone at Fight Health Insurance ({FHI_SUPPORT_EMAIL}) tells "
    "you so.\n"
)


def _describe(request: HttpRequest) -> list[tuple[str, str]]:
    """The request's details, each repr()'d so a newline or control character
    in a client-written header can't forge a log line or an email line."""
    details = [
        ("Method", str(request.method)),
        ("Path", request.get_full_path()),
    ] + [(label, str(request.META.get(key, ""))) for label, key in _REPORTED_META]
    return [(label, repr(value[:_MAX_VALUE_CHARS])) for label, value in details]


def _email_alert(details: list[tuple[str, str]]) -> None:
    if not cache.add(_EMAIL_THROTTLE_KEY, 1, EMAIL_THROTTLE_SECONDS):
        return
    body = (
        f"Something requested {EVIL_ROBOT_ID_PATH}, the path the X-Warning "
        "header points would-be pen testers at to ask for permission.\n\n"
        + "\n".join(f"{label}: {value}" for label, value in details)
        + "\n\nEvery request is in the logs (search for 'evil robot-id'); "
        f"this email is sent at most once every {EMAIL_THROTTLE_SECONDS // 60} "
        "minutes.\n"
    )
    try:
        send_mail(
            "[FHI] Pen test permission requested at /evil/robot-id",
            body,
            settings.DEFAULT_FROM_EMAIL,
            [FHI_SUPPORT_EMAIL],
            fail_silently=False,
        )
    except Exception:
        logger.opt(exception=True).error("Failed to send evil robot-id alert email")


@csrf_exempt
@never_cache
def evil_robot_id_view(request: HttpRequest) -> HttpResponse:
    """Logs the request, emails the support alias, and grants nothing."""
    details = _describe(request)
    logger.warning(
        "evil robot-id request: {}",
        ", ".join(f"{label}={value}" for label, value in details),
    )
    _email_alert(details)
    return HttpResponse(RESPONSE_TEXT, content_type="text/plain; charset=utf-8")
