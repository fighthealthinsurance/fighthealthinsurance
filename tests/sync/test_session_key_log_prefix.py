"""Log lines record at most the first 8 characters of a session key.

The Django session key is the value of the session cookie, so a log line
holding a whole one would let its reader act as that visitor. Each view test
below moves the test client onto a session with a long, distinctive key,
drives one path that logs it, and checks that the line records the key's
first 8 characters and nothing past them.

The last tests read every module in the fighthealthinsurance and fhi_users
packages and fail if a log call formats a session key without passing it
through session_key_prefix_for_log.
"""

import ast
import contextlib
import logging
import pathlib
import re
import typing
import uuid
from importlib import import_module
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse
from loguru import logger
from rest_framework.test import APITestCase

import fhi_users
import fighthealthinsurance
from fighthealthinsurance import common_view_logic
from fighthealthinsurance.log_redaction import (
    SESSION_KEY_LOG_CHARS,
    session_key_prefix_for_log,
)
from fighthealthinsurance.models import ExtraUserProperties, ProfessionalUser

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


# Exactly SESSION_KEY_LOG_CHARS long, so it is the whole of what may be logged.
KEY_PREFIX = "plantkey"
EMAIL = "sessionkeylog@example.com"


def _planted_key() -> str:
    """A 40 character session key (the longest the session table holds)."""
    return KEY_PREFIX + uuid.uuid4().hex


def _plant_session_key(client: Client, key: str) -> None:
    """Move ``client`` onto a stored session whose key is ``key``, carrying
    over what its current session holds (a login, for example)."""
    carried = dict(client.session.items())
    store = import_module(settings.SESSION_ENGINE).SessionStore(session_key=key)
    store.save(must_create=True)
    store.update(carried)
    store.save()
    client.cookies[settings.SESSION_COOKIE_NAME] = key


class _LineCollector(logging.Handler):
    def __init__(self, lines: list):
        super().__init__(level=logging.DEBUG)
        self.lines = lines

    def emit(self, record):
        self.lines.append(self.format(record))


@contextlib.contextmanager
def _captured_log_lines():
    """Every loguru line at DEBUG and above, and every standard logging line
    that reaches the root logger, each with any traceback attached."""
    lines: list = []
    sink_id = logger.add(
        lambda msg: lines.append(str(msg)),
        level="DEBUG",
        format="{message}\n{exception}",
        backtrace=False,
        diagnose=False,
    )
    handler = _LineCollector(lines)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield lines
    finally:
        root.removeHandler(handler)
        logger.remove(sink_id)


_real_create_or_update_denial = (
    common_view_logic.DenialCreatorHelper.create_or_update_denial
)


def _invalid_denial_id_response(*args, **kwargs):
    """Create or update the denial as usual, then report a denial id the
    views refuse, so they log the session key on the way out."""
    response = _real_create_or_update_denial(*args, **kwargs)
    response.denial_id = 0
    return response


class _SessionKeyLogAssertions:
    """Checks shared by the view tests below."""

    assertEqual: typing.Callable
    assertTrue: typing.Callable

    def assert_logs_prefix_only(self, lines, key, message):
        """``message`` was logged with the key's prefix, and no line holds
        the key's first 9 characters or the part of it after the prefix."""
        past_prefix = key[: SESSION_KEY_LOG_CHARS + 1]
        remainder = key[SESSION_KEY_LOG_CHARS:]
        self.assertEqual(
            [line for line in lines if past_prefix in line or remainder in line],
            [],
        )
        self.assertTrue(
            any(
                message in line and f"session_key='{KEY_PREFIX}'" in line
                for line in lines
            ),
            f"no line carries {message!r} with the key prefix: {lines!r}",
        )


class ConsumerFormSessionKeyLogTest(_SessionKeyLogAssertions, TestCase):
    """The consumer denial form and the pages behind SessionRequiredMixin."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        self.client = Client()
        self.key = _planted_key()
        _plant_session_key(self.client, self.key)

    def test_invalid_generated_denial_id_logs_the_key_prefix(self):
        with (
            patch.object(
                common_view_logic.DenialCreatorHelper,
                "create_or_update_denial",
                side_effect=_invalid_denial_id_response,
            ),
            _captured_log_lines() as lines,
            self.assertRaises(ValueError),
        ):
            self.client.post(
                reverse("process"),
                {
                    "email": EMAIL,
                    "denial_text": "Your claim has been denied.",
                    "pii": "on",
                    "tos": "on",
                    "privacy": "on",
                    "personalonly": "on",
                },
            )
        self.assert_logs_prefix_only(
            lines, self.key, "Invalid denial_id generated in form workflow."
        )

    def test_invalid_denial_id_format_logs_the_key_prefix(self):
        with _captured_log_lines() as lines:
            self.client.post(
                reverse("eev"),
                {
                    "denial_id": "not-a-number",
                    "email": EMAIL,
                    "semi_sekret": "sekret",
                },
            )
        self.assert_logs_prefix_only(
            lines,
            self.key,
            "Invalid denial_id format in request context resolution.",
        )

    def test_unknown_denial_reference_logs_the_key_prefix(self):
        with _captured_log_lines() as lines:
            self.client.post(
                reverse("eev"),
                {
                    "denial_id": "987654321",
                    "email": EMAIL,
                    "semi_sekret": "sekret",
                },
            )
        self.assert_logs_prefix_only(
            lines,
            self.key,
            "Invalid denial lookup for provided denial reference.",
        )


class RestSessionKeyLogTest(_SessionKeyLogAssertions, APITestCase):
    """The professional denial API and the client error report."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        self.user = User.objects.create_user(
            username="sessionkeylogpro",
            password="testpass",
            email="sessionkeylogpro@example.com",
        )
        ProfessionalUser.objects.create(
            user=self.user, active=True, npi_number="1234567890"
        )
        ExtraUserProperties.objects.create(user=self.user, email_verified=True)
        self.key = _planted_key()

    def _log_in_on_planted_session(self):
        self.assertTrue(
            self.client.login(username="sessionkeylogpro", password="testpass")
        )
        _plant_session_key(self.client, self.key)

    def _create_denial(self, **extra):
        return self.client.post(
            reverse("denials-list"),
            {
                "email": EMAIL,
                "denial_text": "Your claim has been denied.",
                "pii": "true",
                "tos": "true",
                "privacy": "true",
                **extra,
            },
            format="json",
        )

    def test_invalid_denial_id_format_logs_the_key_prefix(self):
        self._log_in_on_planted_session()
        with _captured_log_lines() as lines:
            response = self._create_denial(denial_id=-5)
        self.assertEqual(response.status_code, 201)
        self.assert_logs_prefix_only(
            lines, self.key, "Invalid denial_id format during denial create/update."
        )

    def test_invalid_denial_id_in_response_logs_the_key_prefix(self):
        self._log_in_on_planted_session()
        with (
            patch.object(
                common_view_logic.DenialCreatorHelper,
                "create_or_update_denial",
                side_effect=_invalid_denial_id_response,
            ),
            _captured_log_lines() as lines,
        ):
            response = self._create_denial()
        self.assertEqual(response.status_code, 400)
        self.assert_logs_prefix_only(
            lines, self.key, "Invalid denial_id in denial create response."
        )

    def test_client_error_report_logs_the_key_prefix(self):
        _plant_session_key(self.client, self.key)
        with _captured_log_lines() as lines:
            response = self.client.post(
                reverse("report_client_error"),
                {"denial_id": "12", "error": "boom"},
                format="json",
            )
        self.assertEqual(response.status_code, 204)
        self.assert_logs_prefix_only(
            lines, self.key, "APPEAL_GEN_DIAG Client-reported appeal error"
        )


class SessionKeyPrefixHelperTest(SimpleTestCase):
    def test_keeps_the_first_8_characters_as_a_repr(self):
        self.assertEqual(
            session_key_prefix_for_log("abcdefghijklmnop"), repr("abcdefgh")
        )

    def test_records_none_when_there_is_no_key(self):
        self.assertEqual(session_key_prefix_for_log(None), "none")
        self.assertEqual(session_key_prefix_for_log(""), "none")


# The guard: a log call may mention a session key only through the helper
# (or bool(), which records whether there is one).

_LOG_METHODS = frozenset(
    {
        "trace",
        "debug",
        "info",
        "success",
        "warning",
        "warn",
        "error",
        "exception",
        "critical",
        "log",
    }
)
_SESSION_KEY_NAME = re.compile(
    r"session_?key|sessionid|^(django_)?session_id$", re.IGNORECASE
)
_SAFE_WRAPPERS = frozenset({"session_key_prefix_for_log", "bool"})


def _called_name(call: ast.Call) -> typing.Optional[str]:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _is_log_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    name = _called_name(node)
    if isinstance(node.func, ast.Name):
        return name == "print"
    return name in _LOG_METHODS


def _session_key_read(node: ast.AST) -> typing.Optional[str]:
    """The name a session key is read through at ``node``, if it is one:
    a variable, an attribute, ``x["session_key"]`` or ``x.get("session_key")``."""
    name: object = None
    if isinstance(node, ast.Name):
        name = node.id
    elif isinstance(node, ast.Attribute):
        name = node.attr
    elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        name = node.slice.value
    elif (
        isinstance(node, ast.Call)
        and _called_name(node) == "get"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    ):
        name = node.args[0].value
    if isinstance(name, str) and _SESSION_KEY_NAME.search(name):
        return name
    return None


def _unwrapped_session_keys(node: ast.AST) -> list:
    """Session key reads under ``node`` that no safe wrapper encloses."""
    if isinstance(node, ast.Call) and _called_name(node) in _SAFE_WRAPPERS:
        return []
    name = _session_key_read(node)
    if name is not None:
        return [name]
    found: list = []
    for child in ast.iter_child_nodes(node):
        found.extend(_unwrapped_session_keys(child))
    return found


def _log_calls_with_bare_session_keys(
    source: str, filename: str
) -> typing.Tuple[list, int]:
    """(line, names) for each log call in ``source`` that formats a session key
    without the helper, and the number of log calls read."""
    offenders = []
    log_calls = 0
    for node in ast.walk(ast.parse(source, filename=filename)):
        if not _is_log_call(node):
            continue
        assert isinstance(node, ast.Call)
        log_calls += 1
        names: list = []
        for arg in [*node.args, *(kw.value for kw in node.keywords)]:
            names.extend(_unwrapped_session_keys(arg))
        if names:
            offenders.append((node.lineno, names))
    return offenders, log_calls


def _package_modules():
    for package in (fighthealthinsurance, fhi_users):
        root = pathlib.Path(package.__file__).parent
        for path in sorted(root.rglob("*.py")):
            if "migrations" not in path.parts:
                yield path


class NoBareSessionKeyInLogCallsTest(SimpleTestCase):
    def test_every_log_call_passes_session_keys_through_the_helper(self):
        offenders = []
        modules = 0
        log_calls = 0
        for path in _package_modules():
            modules += 1
            found, calls = _log_calls_with_bare_session_keys(
                path.read_text(encoding="utf-8"), str(path)
            )
            log_calls += calls
            offenders.extend(f"{path}:{line} {names}" for line, names in found)
        # A scan that reads nothing passes for the wrong reason.
        self.assertGreater(modules, 100)
        self.assertGreater(log_calls, 1000)
        self.assertEqual(
            offenders,
            [],
            "log calls must record session keys through " "session_key_prefix_for_log",
        )

    def test_the_scan_flags_each_way_a_key_reaches_a_log_call(self):
        flagged = [
            'logger.info(f"session_key={session_key}")',
            'logger.opt(exception=True).error(f"{request.session.session_key}")',
            'logger.warning("key %s", server_session_key)',
            "logger.debug(f\"{data.get('session_key')}\")",
            "logger.debug(f\"{data['session_key']}\")",
            "logger.info(f\"{request.COOKIES.get('sessionid')}\")",
            'logger.info(f"{user.django_session_id}")',
            'logger.info(f"{str(session_key)[:8]}")',
            "print(session_key)",
        ]
        for source in flagged:
            with self.subTest(source=source):
                found, calls = _log_calls_with_bare_session_keys(source, "<flagged>")
                self.assertEqual(calls, 1)
                self.assertEqual(len(found), 1)

    def test_the_scan_passes_the_helper_and_bool(self):
        passed = [
            'logger.info(f"session_key={session_key_prefix_for_log(session_key)}")',
            "logger.info(f\"{session_key_prefix_for_log(data.get('session_key'))}\")",
            'logger.debug(f"has_session={bool(session_key)}")',
            'logger.debug("Policy doc session_key fallback: doc_id=3")',
            'logger.info(f"Stripe {stripe_session_id}")',
        ]
        for source in passed:
            with self.subTest(source=source):
                found, calls = _log_calls_with_bare_session_keys(source, "<passed>")
                self.assertEqual(calls, 1)
                self.assertEqual(found, [])
