"""
Tests for the Patient Access & Market Access landing page.

This module tests:
- The patient access route and view
- Page content sections (hero, landing pages, evidence, workflow, compliance, CTA)
- Navigation dropdown functionality
- Links to microsites and microsite directory
"""

from django.test import TestCase, Client
from django.urls import reverse


class PatientAccessViewTest(TestCase):
    """Tests for the patient access landing page view."""

    def setUp(self):
        self.client = Client()

    def test_patient_access_route_loads(self):
        """Test that the patient access route loads successfully."""
        response = self.client.get(reverse("patient_access"))
        self.assertEqual(response.status_code, 200)

    def test_patient_access_uses_correct_template(self):
        """Test that the view uses the correct template."""
        response = self.client.get(reverse("patient_access"))
        self.assertTemplateUsed(response, "patient_access.html")

    def test_patient_access_contains_hero_headline(self):
        """Test that the page contains the hero headline."""
        response = self.client.get(reverse("patient_access"))
        self.assertContains(response, "Appeal Infrastructure for Patient Access Teams")

    def test_patient_access_contains_hero_tagline(self):
        """Test that the page contains the hero tagline."""
        response = self.client.get(reverse("patient_access"))
        self.assertContains(response, "Patient access at scale")

    def test_patient_access_contains_book_demo_cta(self):
        """Test that the page contains the Book a Demo CTA."""
        response = self.client.get(reverse("patient_access"))
        self.assertContains(response, "Book a Demo")

    def test_patient_access_contains_email_cta(self):
        """Test that the page contains the Email Us CTA."""
        response = self.client.get(reverse("patient_access"))
        self.assertContains(response, "Email Us")


class PatientAccessLandingPagesSectionTest(TestCase):
    """Tests for the Landing Pages You Deploy section."""

    def setUp(self):
        self.client = Client()

    def test_contains_landing_pages_section(self):
        """Test that the page contains the landing pages section."""
        response = self.client.get(reverse("patient_access"))
        self.assertContains(response, "AI And Landing Pages")

    def test_contains_drug_specific_content(self):
        """Test that the page mentions drug-specific pages."""
        response = self.client.get(reverse("patient_access"))
        self.assertContains(response, "Drug-Specific Pages")
        self.assertContains(response, "SKYRIZI")

    def test_contains_condition_specific_content(self):
        """Test that the page mentions condition-specific pages."""
        response = self.client.get(reverse("patient_access"))
        self.assertContains(response, "Condition-Specific Pages")
        self.assertContains(response, "Hormone therapy")

    def test_contains_dme_content(self):
        """Test that the page mentions DME pages."""
        response = self.client.get(reverse("patient_access"))
        self.assertContains(response, "DME")
        self.assertContains(response, "CPAP")


class PatientAccessEvidenceSectionTest(TestCase):
    """Tests for the Evidence You Control section."""

    def setUp(self):
        self.client = Client()

    def test_contains_evidence_section(self):
        """Test that the page contains the evidence section."""
        response = self.client.get(reverse("patient_access"))
        self.assertContains(response, "Evidence You Control")

    def test_contains_studies_citations(self):
        """Test that the page mentions peer-reviewed studies."""
        response = self.client.get(reverse("patient_access"))
        self.assertContains(response, "Peer-reviewed studies and citations")

    def test_contains_payer_specific_arguments(self):
        """Test that the page mentions payer-specific arguments."""
        response = self.client.get(reverse("patient_access"))
        self.assertContains(response, "Payer-specific policy arguments")

    def test_contains_internal_clinical_evidence(self):
        """Test that the page mentions internal clinical evidence."""
        response = self.client.get(reverse("patient_access"))
        self.assertContains(response, "Internal clinical evidence")


class PatientAccessWorkflowSectionTest(TestCase):
    """Tests for the Workflow Integration section."""

    def setUp(self):
        self.client = Client()

    def test_contains_workflow_section(self):
        """Test that the page contains the workflow section."""
        response = self.client.get(reverse("patient_access"))
        self.assertContains(response, "Workflow Integration")

    def test_contains_fully_automated_option(self):
        """Test that the page mentions fully automated workflow."""
        response = self.client.get(reverse("patient_access"))
        self.assertContains(response, "Fully Automated")

    def test_contains_human_reviewed_option(self):
        """Test that the page mentions human reviewed workflow."""
        response = self.client.get(reverse("patient_access"))
        self.assertContains(response, "Human Reviewed")

    def test_contains_co_authored_option(self):
        """Test that the page mentions co-authored workflow."""
        response = self.client.get(reverse("patient_access"))
        self.assertContains(response, "Co-Authored")

    def test_contains_approval_gates_option(self):
        """Test that the page mentions approval gates workflow."""
        response = self.client.get(reverse("patient_access"))
        self.assertContains(response, "Approval Gates")

    def test_human_review_is_optional_message(self):
        """Test that the page emphasizes human review is optional."""
        response = self.client.get(reverse("patient_access"))
        self.assertContains(response, "Human review is optional")

    def test_augments_not_replaces_message(self):
        """Test that the page emphasizes augmentation not replacement."""
        response = self.client.get(reverse("patient_access"))
        self.assertContains(response, "augment")


class PatientAccessComplianceSectionTest(TestCase):
    """Tests for the Built for Compliance section."""

    def setUp(self):
        self.client = Client()

    def test_contains_compliance_section(self):
        """Test that the page contains the compliance section."""
        response = self.client.get(reverse("patient_access"))
        self.assertContains(response, "Built for Compliance")


class PatientAccessUseCasesSectionTest(TestCase):
    """Tests for the concrete use cases section."""

    def setUp(self):
        self.client = Client()

    def test_contains_skyrizi_hub_example(self):
        """Test that the page contains the hub team example."""
        response = self.client.get(reverse("patient_access"))
        self.assertContains(response, "hub team")


class PatientAccessLinksTest(TestCase):
    """Tests for links on the patient access page."""

    def setUp(self):
        self.client = Client()

    def test_links_to_biologic_denial_microsite(self):
        """Test that the page links to the biologic denial microsite."""
        response = self.client.get(reverse("patient_access"))
        self.assertContains(response, "/microsite/biologic-denial/")

    def test_contains_enterprise_email(self):
        """Test that the page contains the enterprise email address."""
        response = self.client.get(reverse("patient_access"))
        self.assertContains(response, "support42@fighthealthinsurance.com")

    def test_book_demo_is_mailto_link(self):
        """Test that Book a Demo is a mailto link."""
        response = self.client.get(reverse("patient_access"))
        self.assertContains(response, "mailto:support42@fighthealthinsurance.com")


PROFESSIONAL_MENU = '<summary class="nav-link">Professional</summary>'


class PatientAccessNavigationTest(TestCase):
    """The Professional menu in the header, which is a native <details>."""

    def setUp(self):
        self.client = Client()

    def test_homepage_has_the_professional_menu(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, PROFESSIONAL_MENU)

    def test_the_professional_menu_holds_patient_access(self):
        response = self.client.get("/")
        self.assertContains(response, reverse("patient_access"))
        self.assertContains(response, "Patient &amp; Market Access")

    def test_the_professional_menu_holds_practices(self):
        response = self.client.get("/")
        self.assertContains(response, reverse("pro_version"))
        self.assertContains(response, "Practices &amp; Hospitals")

    def test_patient_access_page_has_the_same_menu(self):
        response = self.client.get(reverse("patient_access"))
        self.assertContains(response, PROFESSIONAL_MENU)

    def test_the_menu_opens_without_bootstraps_javascript(self):
        """A <details> opens by itself. The Bootstrap dropdown it replaced
        needed bootstrap.bundle.min.js from a CDN before it would respond,
        so a blocked or slow script left it dead. None of those hooks may
        come back."""
        response = self.client.get("/")
        self.assertContains(response, '<details class="fhi-nav-group" name="fhi-nav-group">')
        self.assertNotContains(response, 'data-bs-toggle="dropdown"')
        self.assertNotContains(response, "dropdown-toggle")


class PatientAccessAccessibilityTest(TestCase):
    """Tests for accessibility features on the patient access page."""

    def setUp(self):
        self.client = Client()

    def test_page_has_proper_heading_hierarchy(self):
        """Test that the page uses proper heading hierarchy."""
        response = self.client.get(reverse("patient_access"))
        content = response.content.decode("utf-8")
        # Should have h1 in hero, h2 for sections
        self.assertIn("<h1", content)
        self.assertIn("<h2", content)

    def test_the_menu_is_a_native_disclosure(self):
        """<details>/<summary> is a button with an open and closed state
        to a screen reader with no ARIA written by hand, which is what the
        aria-expanded and aria-labelledby on the old dropdown were for."""
        response = self.client.get(reverse("patient_access"))
        self.assertContains(response, '<details class="fhi-nav-group" name="fhi-nav-group">')
        self.assertContains(response, PROFESSIONAL_MENU)


class PatientAccessMetadataTest(TestCase):
    """Tests for page metadata."""

    def setUp(self):
        self.client = Client()

    def test_page_has_title(self):
        """Test that the page has a proper title."""
        response = self.client.get(reverse("patient_access"))
        self.assertContains(response, "<title>")
        self.assertContains(response, "Patient")

    def test_page_has_meta_description(self):
        """Test that the page has a meta description."""
        response = self.client.get(reverse("patient_access"))
        content = response.content.decode("utf-8")
        self.assertIn("meta", content.lower())
        self.assertIn("description", content.lower())
