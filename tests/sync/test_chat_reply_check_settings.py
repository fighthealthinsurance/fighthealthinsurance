"""The chat reply check's settings: off by default, forced off in every
test configuration, demotion of a failed reply on by default and pinned on
under test, and its numeric knobs never crash import."""

import ast
import inspect
import os
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase

from fighthealthinsurance import settings as settings_module
from fighthealthinsurance.settings import _env_float

_KNOBS = {
    "FHI_CHAT_JEV_GATE_MAX_WAIT_SECONDS": 8.0,
    "FHI_CHAT_JEV_GATE_TIMEOUT_SECONDS": 1.5,
    "FHI_CHAT_JEV_GATE_MIN_ANSWERS": 0.7,
    "FHI_CHAT_JEV_GATE_MAX_PROBLEM": 0.3,
}


class ReplyCheckSettingsTest(SimpleTestCase):
    def test_off_in_the_running_test_configuration(self):
        self.assertIs(settings.FHI_CHAT_JEV_GATE_ENABLED, False)

    def test_forced_off_and_pinned_in_every_test_configuration(self):
        """Literal assignments in each test configuration's own body, so a
        developer's environment cannot switch the check on (and send test
        chat text to TypeSafe) in any test run. Read from the source: the
        configuration metaclass copies inherited settings into every class,
        so the class objects cannot tell a pin from an inherited value."""
        tree = ast.parse(inspect.getsource(settings_module))
        classes = {
            node.name: node for node in tree.body if isinstance(node, ast.ClassDef)
        }
        expected = {
            "FHI_CHAT_JEV_GATE_ENABLED": False,
            "FHI_CHAT_JEV_GATE_DEMOTE_FAILED": True,
            **_KNOBS,
        }
        for name in ("Test", "TestSync", "TestActor"):
            pinned = {
                target.id: statement.value.value
                for statement in classes[name].body
                if isinstance(statement, ast.Assign)
                and isinstance(statement.value, ast.Constant)
                for target in statement.targets
                if isinstance(target, ast.Name) and target.id in expected
            }
            self.assertEqual(pinned, expected, name)
            self.assertIs(pinned["FHI_CHAT_JEV_GATE_ENABLED"], False, name)

    def test_off_by_default_outside_tests(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FHI_CHAT_JEV_GATE_ENABLED", None)
            self.assertIs(settings_module._env_flag("FHI_CHAT_JEV_GATE_ENABLED"), False)
        self.assertIs(settings_module.Base.FHI_CHAT_JEV_GATE_ENABLED, False)

    def test_the_base_defaults_are_the_documented_knobs(self):
        for knob, value in _KNOBS.items():
            self.assertEqual(getattr(settings_module.Base, knob), value, knob)

    def test_demotion_is_on_by_default_outside_tests(self):
        """Read from the source, so a developer's environment cannot hide
        the default: Base reads the flag with "1" when it is unset."""
        tree = ast.parse(inspect.getsource(settings_module))
        base = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "Base"
        )
        (reader,) = [
            statement.value
            for statement in base.body
            if isinstance(statement, ast.Assign)
            for target in statement.targets
            if isinstance(target, ast.Name)
            and target.id == "FHI_CHAT_JEV_GATE_DEMOTE_FAILED"
        ]
        self.assertEqual(
            ast.unparse(reader), "_env_flag('FHI_CHAT_JEV_GATE_DEMOTE_FAILED', '1')"
        )
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FHI_CHAT_JEV_GATE_DEMOTE_FAILED", None)
            self.assertIs(
                settings_module._env_flag("FHI_CHAT_JEV_GATE_DEMOTE_FAILED", "1"),
                True,
            )
        self.assertIs(settings.FHI_CHAT_JEV_GATE_DEMOTE_FAILED, True)


class EnvFloatTest(SimpleTestCase):
    def _read(self, default=1.5):
        return _env_float("FHI_TEST_FLOAT", default, minimum=0.2, maximum=10.0)

    def test_parses_a_plain_number(self):
        for raw, value in ((" 2.5 ", 2.5), ("3", 3.0), ("0.2", 0.2), ("10", 10.0)):
            with patch.dict(os.environ, {"FHI_TEST_FLOAT": raw}):
                self.assertEqual(self._read(), value, repr(raw))

    def test_falls_back_on_garbage_or_empty(self):
        for raw in ("1.5s", "", "   ", "fast", "nan", "inf"):
            with patch.dict(os.environ, {"FHI_TEST_FLOAT": raw}):
                self.assertEqual(self._read(), 1.5, repr(raw))

    def test_out_of_range_falls_back(self):
        for raw in ("0", "-1", "0.1", "10.5"):
            with patch.dict(os.environ, {"FHI_TEST_FLOAT": raw}):
                self.assertEqual(self._read(), 1.5, repr(raw))

    def test_falls_back_when_unset(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FHI_TEST_FLOAT", None)
            self.assertEqual(self._read(), 1.5)
