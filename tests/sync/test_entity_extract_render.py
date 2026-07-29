"""entity_extract.html delivers form_context via Django's json_script filter.

Previously the denial-ref dict was spread into an inline <script> under
{% autoescape off %}, so a field value containing an apostrophe or a literal
</script> (e.g. in the email) could break out of the script and stall the
extraction step. These tests pin the safe delivery.
"""

from django.test import TestCase
from django.urls import reverse

from fighthealthinsurance.models import Denial


class EntityExtractFormContextTest(TestCase):
    def setUp(self):
        self.denial = Denial.objects.create(denial_text="Test denial")

    def _get(self, email):
        return self.client.get(
            reverse("eev"),
            {
                "denial_id": str(self.denial.denial_id),
                "email": email,
                "semi_sekret": self.denial.semi_sekret,
            },
        )

    def test_form_context_delivered_via_json_script(self):
        response = self._get("plain@example.com")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="entity-form-context"')

    def test_hostile_email_is_escaped_not_injected(self):
        # An apostrophe used to break the JS string; a </script> used to break
        # the whole script element. Both must be neutralised by json_script.
        hostile = "o'brien</script><script>alert(1)</script>@x.com"
        response = self._get(hostile)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="entity-form-context"')
        # The raw break-out sequences must not appear verbatim in the page.
        self.assertNotContains(response, "o'brien</script>")
        self.assertNotContains(response, "<script>alert(1)</script>@x.com")
