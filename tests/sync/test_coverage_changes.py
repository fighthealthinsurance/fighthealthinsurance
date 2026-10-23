"""The yearly coverage guide lives at one address with no year in it."""

import re

from django.test import Client, TestCase, override_settings
from django.urls import reverse


class CoverageChangesTest(TestCase):
    def test_the_guide_renders(self):
        response = self.client.get(reverse("coverage-changes"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Prepare for 2027 Insurance Changes")

    def test_the_old_yearly_address_sends_people_to_the_guide(self):
        response = self.client.get("/preparing-for-2026")
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], reverse("coverage-changes"))

    def test_the_address_has_no_year_in_it(self):
        # So links and search ranking carry over when the content moves on.
        import re

        self.assertIsNone(re.search(r"20\d\d", reverse("coverage-changes")))

    def test_the_guide_names_its_sources(self):
        html = self.client.get(reverse("coverage-changes")).content.decode()
        for source in ("healthcare.gov/quick-guide/dates-and-deadlines", "rp-26-24.pdf", "2027-announcement.pdf"):
            self.assertIn(source, html)

    def test_end_of_automatic_renewal_with_premium_help_is_expected_not_settled(self):
        # The 2025 law ties premium help to checks from 2028 that CMS hasn't set out yet; KFF says "could effectively end".
        html = self.client.get(reverse("coverage-changes")).content.decode()
        self.assertIn("Automatic renewal with premium help is expected to end for 2028", html)
        self.assertNotIn("premium help ends for 2028", html)

    def test_the_resources_menu_links_to_the_guide(self):
        html = self.client.get(reverse("root")).content.decode()
        self.assertIn(f'href="{reverse("coverage-changes")}"', html)


class CoverageChangesEligibilityLinkTest(TestCase):
    """The guide links to the Medicaid eligibility check only while that page serves."""

    def _guide(self):
        return self.client.get(reverse("coverage-changes")).content.decode()

    @override_settings(MEDICAID_ELIGIBILITY_PAGE_ENABLED=False)
    def test_no_link_to_the_eligibility_check_while_it_is_off(self):
        self.assertNotIn(f'href="{reverse("medicaid-eligibility")}"', self._guide())

    @override_settings(MEDICAID_ELIGIBILITY_PAGE_ENABLED=True)
    def test_links_to_the_eligibility_check_while_it_is_on(self):
        self.assertIn(f'href="{reverse("medicaid-eligibility")}"', self._guide())


def _visible(html: str) -> str:
    """Text as a reader sees it: tags dropped, entities read, spaces collapsed."""
    import html as html_lib
    import re

    return re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", "", html))).strip()


class CoverageChangesFallChecklistTest(TestCase):
    """The fall checklist covers Medicare, Medicaid and employer plans and the Marketplace steps, once each."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        html = Client().get(reverse("coverage-changes")).content.decode()
        start = html.index('<section id="fall-checklist"')
        cls.html = html[start : html.index("</section>", start)]
        cls.text = _visible(cls.html)
        cls.page = html

    def test_keeps_the_medicare_medicaid_prior_auth_and_fsa_items(self):
        for item in (
            "On Medicare, compare plans between October 15 and December 7. Check whether any of your drugs are among the 15 with new negotiated prices.",
            "On Medicaid through expansion, keep your address current and gather proof of your work hours or exemption. If the state says it can't confirm your hours or exemption, you have 30 days from getting that notice to send proof.",
            "Renew or document prior authorizations for ongoing treatment before January 1.",
            "Use your 2026 FSA balance and set your 2027 HSA contributions.",
        ):
            self.assertIn(item, self.text)

    def test_scam_warning_never_says_to_pay_no_one(self):
        # The checklist tells people to pay their premium, so it never says pay no one.
        self.assertNotIn("pay anyone", self.text.lower())

    def test_has_each_marketplace_step_once(self):
        steps = re.findall(r"<li><strong>([^<]+)</strong>", self.html)
        self.assertEqual(len(steps), len(set(steps)))
        for step in (
            "Read your renewal notice.",
            "On the Marketplace, update your income and compare plans between November 1 and December 15",
            "Check what else you qualify for.",
            "Before you buy a plan outside the Marketplace,",
            "Pay your first premium.",
            "Tell the Marketplace when your income changes,",
            "Get free help, and watch for scams.",
            "If the Marketplace says no, you can appeal.",
        ):
            self.assertEqual(steps.count(step), 1, step)
        # The renewal letters and the income update each live in one item, not a second copy.
        self.assertEqual(self.text.count("update your income"), 1)
        self.assertEqual(self.text.count("two letters by November 1"), 1)
        self.assertEqual(self.text.count("reconcile them (Form 8962)"), 1)

    def test_links_each_page_it_relies_on(self):
        for href in (
            "https://www.healthcare.gov/keep-or-change-plan/",
            "https://www.healthcare.gov/marketplace-in-your-state/",
            "https://www.healthcare.gov/medicaid-chip/",
            "https://www.healthcare.gov/have-job-based-coverage/options/",
            "https://www.healthcare.gov/immigrants/lawfully-present-immigrants/",
            reverse("state_help_index"),
            "https://www.healthcare.gov/apply-and-enroll/complete-your-enrollment/",
            "https://www.healthcare.gov/reporting-changes/",
            "https://www.irs.gov/tax-professionals/eitc-central/repayment-caps-eliminated-for-excess-advance-payments-of-the-premium-tax-credit",
            "https://www.healthcare.gov/find-local-help/",
            "https://www.healthcare.gov/protect-from-fraud-and-scams/",
            "https://www.healthcare.gov/marketplace-appeals/",
        ):
            self.assertIn(f'href="{href}"', self.html)

    def test_outside_links_open_safely_like_the_rest_of_the_page(self):
        for tag in re.findall(r'<a href="https://[^"]+"[^>]*>', self.html):
            self.assertIn('target="_blank" rel="noopener noreferrer"', tag)

    def test_gives_the_figures_checked_live(self):
        for figure in (
            "out-of-pocket limit for covered, in-network care can be as high as $12,000 for one person and $24,000 for a family",
            "1-800-318-2596 (TTY 1-855-889-4325), 24 hours a day, 7 days a week, except holidays",
            "Starting with 2026 taxes, if you got more premium help than you qualified for, you pay all the extra back. There's no cap anymore.",
            "You generally have 90 days from your eligibility notice",
            "still get savings starting January 1, 2027",
        ):
            self.assertIn(figure, self.text)

    def test_quotes_are_healthcare_govs_words(self):
        for quote in (
            '"plan costs can change,"',
            '"your coverage won\'t start until you pay your first premium."',
            '"you may no longer qualify for savings on your Marketplace plan even if you don\'t accept"',
            '"The Marketplace, assisters, and your state will never threaten you or your household or ask for your credit card information or payment to keep or qualify for health coverage."',
        ):
            self.assertIn(quote, self.text)

    def test_phone_numbers_can_be_dialled(self):
        calls = re.findall(r'href="tel:\+1(\d{10})">([^<]+)</a>', self.html)
        self.assertEqual(len(calls), 2)
        for href, shown in calls:
            self.assertEqual(href, re.sub(r"\D", "", shown)[-10:], shown)

    def test_phone_numbers_stay_on_one_line(self):
        self.assertRegex(self.page, r'\.prep-checklist a\[href\^="tel:"\] \{\s*white-space: nowrap;')

    def test_help_points_to_independent_assisters(self):
        self.assertIn('the tool lists agents and brokers too, so pick "Independent assisters"', self.text)

    def test_appeal_item_says_insurer_denials_are_a_different_appeal(self):
        self.assertIn("If your insurer says no to care or a claim, that's a different appeal, the one we help with.", self.text)

    def test_no_em_dash_and_no_insurer_named(self):
        import json
        import pathlib

        self.assertNotIn(chr(0x2014), self.html)
        bundle = pathlib.Path(__file__).resolve().parents[2] / "fighthealthinsurance" / "static" / "denial_watch.json"
        for issuer in json.loads(bundle.read_text())["issuers"]:
            self.assertNotIn(re.sub(r",? Inc\.?$", "", issuer["n"]), self.text)

    def test_says_when_it_was_last_updated(self):
        self.assertIn("Last updated: October 2026.", self.page)
