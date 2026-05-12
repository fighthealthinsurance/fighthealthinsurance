"""Selenium tests for the pharmacy-coupon section.

Two surfaces are exercised:

  * Microsite landing page (drug-focused): the GLP-1 / Wegovy microsite
    triggers ``Microsite.pharmacy_coupon_suggestion()``, which surfaces the
    "Pharmacy Discount Options" section through the shared
    ``partials/pharmacy_coupon_section.html`` partial.

  * Consumer denial flow (Step 6 / outside_help.html): pre-create a
    denial whose procedure mentions Truvada (a brand we flag as expensive
    via ``EXPENSIVE_DRUGS_ORDERED``) and hit the ``find_next_steps``
    URL via GET so the same partial renders alongside the
    "Additional resources that might help" table.
"""

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from seleniumbase import BaseCase

from fighthealthinsurance.models import Denial

from .fhi_selenium_base import FHISeleniumBase

BaseCase.main(__name__, __file__)


class SeleniumTestPharmacyCouponSection(FHISeleniumBase, StaticLiveServerTestCase):
    """End-to-end coverage for the pharmacy_coupon_section partial."""

    fixtures = [
        "fighthealthinsurance/fixtures/initial.yaml",
        "fighthealthinsurance/fixtures/followup.yaml",
        "fighthealthinsurance/fixtures/plan_source.yaml",
    ]

    @classmethod
    def setUpClass(cls):
        super(StaticLiveServerTestCase, cls).setUpClass()
        super(BaseCase, cls).setUpClass()

    @classmethod
    def tearDownClass(cls):
        super(StaticLiveServerTestCase, cls).tearDownClass()
        super(BaseCase, cls).tearDownClass()

    # ------------------------------------------------------------------
    # Microsite path: GLP-1 / Wegovy
    # ------------------------------------------------------------------
    def test_microsite_wegovy_shows_pharmacy_discount_options(self):
        """The Wegovy microsite renders the Pharmacy Discount Options section
        with all three pharmacies and the OOP-max caveat."""
        self.open(
            f"{self.live_server_url}/microsite/glp1-denial-semaglutide-wegovy"
        )
        self.wait_for_page_ready()

        # Section + heading rendered.
        self.assert_element("section#pharmacy-coupons")
        self.assert_text("Pharmacy Discount Options", "section#pharmacy-coupons")

        # All three discount pharmacies surfaced. The Amazon option is
        # labeled "Amazon Search" because the affiliate URL points at
        # amazon.com/s rather than the prescription pharmacy.amazon.com.
        self.assert_text("GoodRx", "section#pharmacy-coupons")
        self.assert_text("Mark Cuban Cost Plus Drugs", "section#pharmacy-coupons")
        self.assert_text("Amazon Search", "section#pharmacy-coupons")

        # The Wegovy-specific bridge message warns the user not to rely on
        # discounts (it's an expensive specialty drug).
        body = self.get_text("section#pharmacy-coupons")
        assert "expensive" in body.lower(), (
            "Wegovy bridge message should warn the drug is expensive; "
            f"got: {body!r}"
        )

        # OOP-max caveat is non-negotiable - cash-pay does not count
        # toward the patient's deductible, and the partial must surface that.
        assert "out-of-pocket maximum" in body.lower()

        # Amazon link uses our affiliate template.
        amazon_link = self.find_element("section#pharmacy-coupons a[href*='amazon']")
        href = amazon_link.get_attribute("href") or ""
        assert "tag=totallylegitco-20" in href, (
            f"Amazon link should carry the affiliate tag; got {href!r}"
        )

    def test_microsite_non_drug_omits_pharmacy_section(self):
        """A non-drug microsite (mri-denial) should NOT render the pharmacy
        section at all - the partial gates on a detected drug."""
        self.open(f"{self.live_server_url}/microsite/mri-denial")
        self.wait_for_page_ready()
        # Section should be absent entirely - the partial renders nothing
        # when pharmacy_coupon_suggestion returns None.
        assert (
            len(self.find_elements("section#pharmacy-coupons")) == 0
        ), "MRI microsite should not surface a pharmacy section"

    # ------------------------------------------------------------------
    # Consumer denial flow: Truvada via find_next_steps GET
    # ------------------------------------------------------------------
    def test_truvada_denial_shows_pharmacy_section_in_next_steps(self):
        """A Denial whose procedure is Truvada renders the Pharmacy Discount
        Options section on Step 6 (outside_help.html) when the user navigates
        back via the find_next_steps URL."""
        email = "truvada-denial@example.com"
        hashed_email = Denial.get_hashed_email(email)
        denial = Denial.objects.create(
            denial_text=(
                "Your request for Truvada (emtricitabine/tenofovir disoproxil) "
                "has been denied as non-formulary."
            ),
            procedure="Truvada",
            diagnosis="HIV pre-exposure prophylaxis",
            hashed_email=hashed_email,
            raw_email=email,
            health_history="",
            use_external=False,
        )

        url = (
            f"{self.live_server_url}/find_next_steps"
            f"?denial_id={denial.denial_id}"
            f"&email={email}"
            f"&semi_sekret={denial.semi_sekret}"
        )
        self.open(url)
        self.wait_for_page_ready()

        # Pharmacy section should render in the consumer flow too.
        self.assert_element("section#pharmacy-coupons")
        body = self.get_text("section#pharmacy-coupons")
        assert "GoodRx" in body
        assert "Mark Cuban Cost Plus Drugs" in body
        assert "Amazon Search" in body
        # Truvada is flagged expensive (combo brand), so the bridge message
        # warns the appeal is the primary path.
        assert "expensive" in body.lower(), (
            f"Truvada bridge message should warn expensive; got: {body!r}"
        )
        assert "out-of-pocket maximum" in body.lower()
