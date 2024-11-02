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
            r = m.assemble_outputs(user_header="MyHeader", extra="", input_paths=[t.name])
            self.assertEquals(len(r), 1)
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
            r = m.assemble_outputs(user_header="MyHeader", extra="", input_paths=[t1.name])
            self.assertEquals(len(r), 2)
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
                self.assertEquals(len(r), 1)
                reader = PdfReader(r[0])
                header_text = reader.pages[0].extract_text()
                self.assertIn("MyHeader", header_text)
                text = reader.pages[1].extract_text()
                self.assertIn("Test", text)
                text = reader.pages[2].extract_text()
                self.assertIn("Sup", text)
