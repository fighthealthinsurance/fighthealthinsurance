import os
from os import environ
import unittest
from fighthealthinsurance.fax_utils import FaxSenderBase, FlexibleFaxMagic
import tempfile
import time
import pytest
from PyPDF2 import PdfReader


class FaxSendBaseTest(unittest.TestCase):
    def test_parse_phone_number(self):
        f = FaxSenderBase()
        bad_numbers = ["911", "211", "+0", "+0123", "+1911", "+19002335555"]
        ok_numbers = ["14255555555", "4255555555", "+1-425-555-5555", "+1 425 555 5555"]
        for number in bad_numbers:
            with pytest.raises(Exception):
                f.parse_phone_number(number)
        for number in ok_numbers:
            f.parse_phone_number(number)

    def test_assemble_basic_input(self):
        m = FlexibleFaxMagic([])
        with tempfile.NamedTemporaryFile(
            suffix=".txt", prefix="meeps", mode="w+t", delete=True
        ) as t:
            t.write("Test")
            t.flush()
            os.sync()
            print(f"Using temp file {t.name}")
            r = m.assemble_outputs(
                user_header="MyHeader", extra="", input_paths=[t.name]
            )
            self.assertEqual(len(r), 1)
            reader = PdfReader(r[0])
            header_text = reader.pages[0].extract_text()
            self.assertIn("MyHeader", header_text)
            text = reader.pages[1].extract_text()
            self.assertIn("Test", text)

    def test_assemble_longer_input(self):
        m = FlexibleFaxMagic([])
        with tempfile.NamedTemporaryFile(
            suffix=".txt", prefix="long_meeps", mode="w+t", delete=True
        ) as t1:
            for i in range(0, 10000):
                t1.write("Test ")
            t1.flush()
            time.sleep(1)
            r = m.assemble_outputs(
                user_header="MyHeader", extra="", input_paths=[t1.name]
            )
            self.assertEqual(len(r), 2)
            reader = PdfReader(r[0])
            header_text = reader.pages[0].extract_text()
            self.assertIn("MyHeader", header_text)
            self.assertIn("1 of 2", header_text)
            text = reader.pages[1].extract_text()
            self.assertIn("Test", text)
            reader = PdfReader(r[1])
            header_text = reader.pages[0].extract_text()
            self.assertIn("MyHeader", header_text)
            self.assertIn("2 of 2", header_text)

    def test_assemble_multi_input(self):
        m = FlexibleFaxMagic([])
        with tempfile.NamedTemporaryFile(
            suffix=".txt", prefix="short_meeps", mode="w+t", delete=True
        ) as t1:
            for i in range(0, 10):
                t1.write("Test ")
            t1.flush()
            time.sleep(1)
            with tempfile.NamedTemporaryFile(
                suffix=".txt", prefix="short_meeps", mode="w+t", delete=False
            ) as t2:
                for i in range(0, 10):
                    t2.write("Sup ")
                    t2.flush()
                r = m.assemble_outputs("MyHeader", "", [t1.name, t2.name])
                self.assertEqual(len(r), 1)
                reader = PdfReader(r[0])
                header_text = reader.pages[0].extract_text()
                self.assertIn("MyHeader", header_text)
                text = reader.pages[1].extract_text()
                self.assertIn("Test", text)
                text = reader.pages[2].extract_text()
                self.assertIn("Sup", text)

    def test_professional_backends_filter(self):
        """Test that professional_backends property correctly filters backends"""
        # Create backends with different professional settings
        regular_backend = FaxSenderBase()
        regular_backend.professional = False

        pro_backend = FaxSenderBase()
        pro_backend.professional = True

        # Create FlexibleFaxMagic with mixed backends
        fax_magic = FlexibleFaxMagic([regular_backend, pro_backend])

        # Check professional backends filter
        pro_backends = fax_magic.professional_backends
        self.assertEqual(len(pro_backends), 1)
        self.assertTrue(pro_backends[0].professional)

    def test_send_fax_uses_professional_backends(self):
        """Test that send_fax uses professional backends when professional flag is True"""

        # Create mock backends for testing
        class MockFaxSenderBase(FaxSenderBase):
            def __init__(self, professional=False, name="mock"):
                self.professional = professional
                self.name = name
                self.called = False

            async def send_fax(self, destination, path, dest_name=None, blocking=False):
                self.called = True
                return True

            def estimate_cost(self, destination, pages):
                return 10  # Same cost for both backends

        regular_backend = MockFaxSenderBase(professional=False, name="regular")
        pro_backend = MockFaxSenderBase(professional=True, name="pro")

        # Original _send_fax method to replace after test
        original_send_fax = FlexibleFaxMagic._send_fax

        with tempfile.NamedTemporaryFile(
            suffix=".txt", prefix="short_meeps", mode="w+t", delete=True
        ) as t1:
            t1.write("Test ")
            t1.flush()
            time.sleep(1)
            try:
                # Create a subclass to track which backends are selected
                class TestFlexibleFaxMagic(FlexibleFaxMagic):
                    last_used_backends = None

                    async def _send_fax(
                        self, path, destination, blocking, professional
                    ):
                        # Record which backends would be used
                        if professional:
                            self.last_used_backends = self.professional_backends
                        else:
                            self.last_used_backends = self.backends
                        return True

                # Test with professional=False
                fax_magic = TestFlexibleFaxMagic([regular_backend, pro_backend])
                import asyncio

                asyncio.run(
                    fax_magic.send_fax(
                        input_paths=[t1.name],
                        extra="",
                        destination="14255555555",
                        blocking=False,
                        professional=False,
                    )
                )

                # Should use all backends
                self.assertEqual(len(fax_magic.last_used_backends), 2)

                # Test with professional=True
                asyncio.run(
                    fax_magic.send_fax(
                        input_paths=[t1.name],
                        extra="",
                        destination="14255555555",
                        blocking=False,
                        professional=True,
                    )
                )

                # Should only use professional backends
                self.assertEqual(len(fax_magic.last_used_backends), 1)
                self.assertTrue(fax_magic.last_used_backends[0].professional)

            finally:
                # Restore original method
                FlexibleFaxMagic._send_fax = original_send_fax
