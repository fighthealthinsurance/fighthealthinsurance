import os
from os import environ
import unittest
from fighthealthinsurance.fax_utils import SonicFax
import tempfile
import pytest


class SonicFaxTest(unittest.TestCase):

    def _sonic_is_configured() -> bool:
        keys = ["SONIC_USERNAME", "SONIC_PASSWORD", "SONIC_TOKEN"]
        for key in keys:
            if key not in environ:
                return True
        return False

    # @pytest.mark.skipif(_sonic_is_configured(), reason="Not configured")
    @pytest.mark.skip
    def test_sonic_fax_success(self):
        s = SonicFax()
        with tempfile.NamedTemporaryFile(
            suffix=".txt", prefix="meeps", mode="w+t", delete=False
        ) as f:
            # Write content to the file
            f.write("This is a test fax")
            f.close()
            os.sync()

            # Get the file name
            file_name = f.name
            self.assertTrue(
                s.send_fax(
                    destination=os.getenv("TEST_GOOD_FAX_NUMBER", "4158407591"),
                    path=file_name,
                    blocking=True,
                )
            )

    # @pytest.mark.skipif(_sonic_is_configured(), reason="Not configured")
    @pytest.mark.skip
    def test_sonic_fax_failure(self):
        s = SonicFax()
        with tempfile.NamedTemporaryFile(
            suffix=".txt", prefix="meeps", mode="w+t", delete=False
        ) as f:
            # Write content to the file
            f.write("This is a test fax")
            f.close()
            os.sync()

            # Get the file name
            file_name = f.name
            self.assertFalse(
                s.send_fax(
                    destination=os.getenv("TEST_BAD_FAX_NUMBER", "4255555555"),
                    path=file_name,
                    blocking=True,
                )
            )
