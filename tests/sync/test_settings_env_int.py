"""settings._env_int: an optional integer setting must never crash import."""

import os
from unittest.mock import patch

from django.test import SimpleTestCase

from fighthealthinsurance.settings import _env_int


class EnvIntTest(SimpleTestCase):
    def _read(self, default=7):
        return _env_int("FHI_TEST_INT", default, minimum=1, maximum=300)

    def test_parses_a_plain_integer(self):
        with patch.dict(os.environ, {"FHI_TEST_INT": " 42 "}):
            self.assertEqual(self._read(), 42)

    def test_falls_back_on_garbage_or_empty(self):
        for raw in ("20s", "", "   ", "twenty"):
            with patch.dict(os.environ, {"FHI_TEST_INT": raw}):
                self.assertEqual(self._read(), 7, repr(raw))

    def test_out_of_range_falls_back_so_a_zero_timeout_never_means_no_timeout(self):
        for raw in ("0", "-5", "301"):
            with patch.dict(os.environ, {"FHI_TEST_INT": raw}):
                self.assertEqual(self._read(), 7, repr(raw))

    def test_falls_back_when_unset(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FHI_TEST_INT", None)
            self.assertEqual(self._read(), 7)
