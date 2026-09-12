"""The upload page offers the on-device advanced OCR option only when
ADVANCED_OCR_OFFERED is on; the option is hidden, not disabled, while the
engine is broken."""

from django.test import TestCase, override_settings
from django.urls import reverse


class AdvancedOcrOfferedTest(TestCase):
    @override_settings(ADVANCED_OCR_OFFERED=False)
    def test_the_option_is_absent_when_not_offered(self):
        # Explicit, so an ADVANCED_OCR_OFFERED in the caller's environment
        # cannot fail this; the default itself is pinned in the async-unit
        # source test (review).
        response = self.client.get(reverse("scan"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'id="advanced_ocr_enabled"')
        self.assertNotContains(response, "advanced_ocr_section")

    @override_settings(ADVANCED_OCR_OFFERED=True)
    def test_the_option_appears_unchecked_when_offered(self):
        response = self.client.get(reverse("scan"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="advanced_ocr_enabled"')
        html = response.content.decode()
        tag = html[html.index('<input type="checkbox" id="advanced_ocr_enabled"') :]
        tag = tag[: tag.index(">") + 1]
        self.assertNotIn("checked", tag)
        self.assertContains(response, "Better text recognition for photos and scans")
        self.assertNotContains(response, "huggingface")
