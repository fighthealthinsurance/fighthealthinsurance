"""Log tags (log_tags.py) and the command that finds a data removal's tag."""

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase

from fighthealthinsurance.log_tags import email_tag, error_text, file_tag

EMAIL = "rebecca.crumpler@example.com"


class LogTagsTest(SimpleTestCase):
    def test_a_file_always_gets_the_same_tag(self):
        self.assertEqual(
            file_tag("Rebecca_Crumpler_SBC.pdf"), file_tag("Rebecca_Crumpler_SBC.pdf")
        )

    def test_different_files_get_different_tags(self):
        self.assertNotEqual(file_tag("a.pdf"), file_tag("b.pdf"))

    def test_the_tag_depends_on_the_secret_key(self):
        with self.settings(SECRET_KEY="another-key-only-for-this-test"):
            other = file_tag("a.pdf")
        self.assertNotEqual(file_tag("a.pdf"), other)

    def test_an_email_tag_ignores_case_and_spaces(self):
        self.assertEqual(email_tag(" Rebecca.Crumpler@Example.com "), email_tag(EMAIL))

    def test_an_os_error_keeps_its_reason_but_not_its_path(self):
        e = PermissionError(13, "Permission denied", "/media/Rebecca_Crumpler_SBC.pdf")
        self.assertEqual(error_text(e), "PermissionError: Permission denied")

    def test_other_errors_keep_only_their_type(self):
        self.assertEqual(
            error_text(ValueError("Rebecca_Crumpler_SBC.pdf")), "ValueError"
        )


class RemovalLogTagCommandTest(SimpleTestCase):
    def test_it_prints_the_tag_for_the_email_given(self):
        out = StringIO()
        call_command("removal_log_tag", EMAIL, stdout=out)
        self.assertEqual(out.getvalue().strip(), email_tag(EMAIL))

    def test_it_asks_for_the_email_when_none_is_given(self):
        out = StringIO()
        with patch("builtins.input", return_value=EMAIL):
            call_command("removal_log_tag", stdout=out)
        self.assertEqual(out.getvalue().strip(), email_tag(EMAIL))
