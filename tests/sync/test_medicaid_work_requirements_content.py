"""The Medicaid work-requirements FAQ, blog post and chat answer get the law right.

The FAQ and the blog post are markdown that blog_post.tsx renders in the
browser, so these read the files. Facts are from Public Law 119-21, Sec.
71119, and CMS's June 2026 rule (42 CFR 435.550 to 435.563).
"""

from pathlib import Path

from django.test import SimpleTestCase

from fighthealthinsurance.ml.ml_models import MEDICAID_WORK_REQUIREMENTS_BLOCK

STATIC = Path(__file__).resolve().parents[2] / "fighthealthinsurance" / "static"
FAQ = (STATIC / "faq" / "medicaid-work-requirements-faq.md").read_text()
BLOG = (STATIC / "blog" / "medicaid-work-requirements.md").read_text()


def _section(text: str, anchor: str) -> str:
    """The text under the heading carrying ``{#anchor}``, up to the next heading."""
    start = text.index("{#" + anchor + "}")
    end = text.find("\n### ", start)
    return text[start : end if end != -1 else len(text)]


class TestMedicaidFaq(SimpleTestCase):
    def test_school_and_unemployment_are_not_exemptions(self):
        # Half-time school is a way to meet the rule, and unemployment pay
        # counts as income; neither exempts anyone.
        exempt = _section(FAQ, "who-is-exempt").lower()
        self.assertNotIn("half-time", exempt)
        self.assertNotIn("unemployment benefits", exempt)
        self.assertIn("half-time", _section(FAQ, "how-do-i-meet-it"))

    def test_exemptions_apply_in_every_state(self):
        self.assertIn("exempts these groups in every state", FAQ)

    def test_expansion_count_is_forty_states_and_dc(self):
        self.assertIn("40 states and Washington, D.C.", FAQ)

    def test_it_says_how_to_keep_coverage_during_an_appeal(self):
        self.assertIn("ask for a hearing before the end date on your notice", FAQ)

    def test_it_says_when_it_was_checked(self):
        self.assertIn("*Checked ", FAQ)

    def test_school_hours_count_in_the_mix(self):
        # (xx)(2)(E): any combination of work, service, work program and school.
        self.assertIn("job training, school, or volunteering", FAQ)

    def test_it_does_not_promise_ninety_days_for_a_hearing(self):
        # 42 CFR 431.221(d) caps it at 90; states can set less.
        self.assertNotIn("up to 90 days", FAQ)


class TestMedicaidBlogPost(SimpleTestCase):
    """Dated post: only what was wrong when it ran is corrected, with a note."""

    def test_expansion_count_corrected(self):
        self.assertNotIn("41 states", BLOG.split("*Correction")[0])
        self.assertIn("40 states and Washington, D.C.", BLOG)

    def test_hardship_no_longer_covers_family_emergencies(self):
        self.assertNotIn("family emergency", BLOG)

    def test_correction_note_says_what_changed(self):
        self.assertIn("*Correction, October 2026:", BLOG)

    def test_it_names_the_other_ways_to_meet_the_rule(self):
        self.assertIn("Going to school at least half-time, or earning", BLOG)


class TestChatWorkRequirementsAnswer(SimpleTestCase):
    def test_it_names_exemptions_that_apply_everywhere(self):
        self.assertIn("Exempt in every state", MEDICAID_WORK_REQUIREMENTS_BLOCK)
        self.assertIn("13 or younger", MEDICAID_WORK_REQUIREMENTS_BLOCK)

    def test_it_says_how_to_keep_coverage_during_an_appeal(self):
        self.assertIn(
            "ask for a fair hearing before the end date on your notice",
            MEDICAID_WORK_REQUIREMENTS_BLOCK,
        )

    def test_the_thirty_days_are_for_a_noncompliance_notice(self):
        # A termination notice can give only 10 days (42 CFR 431.211).
        self.assertIn(
            "If your state says you haven't met the rule, you have 30 days",
            MEDICAID_WORK_REQUIREMENTS_BLOCK,
        )
