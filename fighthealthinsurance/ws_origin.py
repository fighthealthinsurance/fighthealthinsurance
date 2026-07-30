"""Cross-site WebSocket origin validation that tolerates originless clients.

Browsers ALWAYS send an Origin header on WebSocket handshakes, so cross-site
WebSocket hijacking -- the attack origin validation exists to stop -- always
arrives WITH a (hostile) Origin. Non-browser clients (curl smoke tests,
native apps, uptime probes) often send none and carry no ambient cookies, so
rejecting them adds breakage without adding protection.

Channels' stock ``AllowedHostsOriginValidator`` rejects a missing Origin
unless ``ALLOWED_HOSTS`` contains ``"*"`` (which production deliberately does
not). This validator allows a missing Origin while still validating supplied
ones against ``ALLOWED_HOSTS``.
"""

from channels.security.websocket import OriginValidator
from django.conf import settings


class BrowserOriginValidator(OriginValidator):
    """OriginValidator that allows handshakes with no Origin header."""

    def valid_origin(self, parsed_origin):
        if parsed_origin is None:
            return True
        return super().valid_origin(parsed_origin)


def allowed_hosts_browser_origin_validator(application):
    """Like channels' AllowedHostsOriginValidator, but originless clients are
    allowed (same ALLOWED_HOSTS sourcing, including the DEBUG default)."""
    allowed_hosts = settings.ALLOWED_HOSTS
    if settings.DEBUG and not allowed_hosts:
        allowed_hosts = ["localhost", "127.0.0.1", "[::1]"]
    return BrowserOriginValidator(application, allowed_hosts)
