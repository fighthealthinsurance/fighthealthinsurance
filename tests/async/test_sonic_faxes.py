import os
from os import environ
import unittest
import tempfile
import asyncio
from fighthealthinsurance.fax_utils import SonicFax


def _sonic_is_configured() -> bool:
    """Check if Sonic credentials are configured in environment."""
    keys = ["SONIC_USERNAME", "SONIC_PASSWORD", "SONIC_TOKEN"]
    return all(key in environ for key in keys)


class SonicFaxTest(unittest.TestCase):

    @unittest.skipUnless(_sonic_is_configured(), "Sonic credentials not configured")
    def test_sonic_fax_success(self):
        """Test faxing with a valid fax number."""
        s = SonicFax()
        with tempfile.NamedTemporaryFile(
            suffix=".txt", prefix="meeps", mode="w+t", delete=False
        ) as f:
            f.write("This is a test fax")
            f.close()
            os.sync()

            file_name = f.name
            try:

                result = asyncio.run(
                    s.send_fax(
                        destination=os.getenv("TEST_GOOD_FAX_NUMBER", "4158407591"),
                        path=file_name,
                        blocking=True,
                    )
                )
                self.assertTrue(result)
            finally:
                os.remove(file_name)

    @unittest.skipUnless(_sonic_is_configured(), "Sonic credentials not configured")
    def test_sonic_fax_failure(self):
        """Test faxing with an invalid fax number."""
        s = SonicFax()
        with tempfile.NamedTemporaryFile(
            suffix=".txt", prefix="meeps", mode="w+t", delete=False
        ) as f:
            f.write("This is a test fax")
            f.close()
            os.sync()

            file_name = f.name
            try:
                result = asyncio.run(
                    s.send_fax(
                        destination=os.getenv("TEST_BAD_FAX_NUMBER", "4255555555"),
                        path=file_name,
                        blocking=True,
                    )
                )
                self.assertFalse(result)
            finally:
                os.remove(file_name)

    @unittest.skipUnless(_sonic_is_configured(), "Sonic credentials not configured")
    def test_invalid_file(self):
        """Test sending an invalid file format."""
        s = SonicFax()
        with tempfile.NamedTemporaryFile(
            suffix=".pdf", prefix="meeps", mode="w+t", delete=False
        ) as f:
            f.write("This is an invalid fax file for testing.")
            f.close()
            os.sync()

            file_name = f.name
            try:
                result = asyncio.run(
                    s.send_fax(
                        destination=os.getenv("TEST_GOOD_FAX_NUMBER", "4158407591"),
                        path=file_name,
                        blocking=True,
                    )
                )
                self.assertFalse(result)
            finally:
                os.remove(file_name)

    @unittest.skipUnless(_sonic_is_configured(), "Sonic credentials not configured")
    def test_empty_file(self):
        """Test sending an empty file."""
        s = SonicFax()
        with tempfile.NamedTemporaryFile(
            suffix=".txt", prefix="empty_meeps", mode="w+t", delete=False
        ) as f:
            f.write("")
            f.close()
            os.sync()

            file_name = f.name
            try:
                result = asyncio.run(
                    s.send_fax(
                        destination=os.getenv("TEST_GOOD_FAX_NUMBER", "4158407591"),
                        path=file_name,
                        blocking=True,
                    )
                )
                self.assertFalse(result)
            finally:
                os.remove(file_name)
