"""Tests for MedicationContext model and AppealGenerator.

_collect_medication_context injection."""

from django.test import TestCase

from fighthealthinsurance.generate_appeal import AppealGenerator
from fighthealthinsurance.models import Denial, MedicationContext


class MedicationContextModelTests(TestCase):
    """Test the MedicationContext model and matches() helper."""

    def setUp(self):
        # Clear the rows created by the 0166 seed migration so this test class
        # exercises only the rows it constructs. Inside Django's test
        # transaction, the seed rows reappear after rollback.
        MedicationContext.objects.all().delete()
        self.glp1 = MedicationContext.objects.create(
            drug_class="GLP-1 receptor agonist",
            brand_names="Ozempic\nWegovy\nMounjaro",
            generic_names="semaglutide\ntirzepatide",
            regex=r"(ozempic|wegovy|mounjaro|semaglutide|tirzepatide)",
            fda_indications="Type 2 diabetes; chronic weight management.",
            common_denial_reasons="Cosmetic; step therapy; quantity limits.",
            appeal_context="Frame obesity as a chronic disease.",
            pubmed_ids="37952131\n33567185",
        )
        self.cgrp = MedicationContext.objects.create(
            drug_class="Anti-CGRP monoclonal antibody",
            brand_names="Aimovig\nAjovy\nEmgality\nVyepti",
            regex=r"(aimovig|ajovy|emgality|vyepti|erenumab|fremanezumab)",
            appeal_context="Document monthly migraine days from a diary.",
        )

    def test_matches_brand_name(self):
        self.assertTrue(self.glp1.matches("Patient is on Ozempic"))

    def test_matches_generic_name(self):
        self.assertTrue(self.glp1.matches("semaglutide 1mg weekly"))

    def test_matches_is_case_insensitive(self):
        self.assertTrue(self.glp1.matches("OZEMPIC PEN"))

    def test_no_match_for_unrelated_drug(self):
        self.assertFalse(self.glp1.matches("lisinopril 10mg"))

    def test_no_match_for_empty_text(self):
        self.assertFalse(self.glp1.matches(""))

    def test_no_match_for_none(self):
        self.assertFalse(self.glp1.matches(None))

    def test_separate_classes_match_independently(self):
        self.assertTrue(self.cgrp.matches("Trying Vyepti for migraines"))
        self.assertFalse(self.cgrp.matches("Trying Ozempic"))

    def test_active_flag_filters_queryset(self):
        self.glp1.active = False
        self.glp1.save()
        active = MedicationContext.objects.filter(active=True)
        self.assertNotIn(self.glp1, list(active))
        self.assertIn(self.cgrp, list(active))


class CollectMedicationContextTests(TestCase):
    """Test AppealGenerator._collect_medication_context against Denial inputs."""

    def setUp(self):
        # Clear the rows created by the 0166 seed migration so the regex
        # matching and inactive-flag assertions below remain deterministic.
        MedicationContext.objects.all().delete()
        MedicationContext.objects.create(
            drug_class="GLP-1 receptor agonist",
            regex=r"(ozempic|wegovy|mounjaro|zepbound|semaglutide|tirzepatide)",
            fda_indications="Type 2 diabetes; weight management.",
            common_denial_reasons="Cosmetic; step therapy.",
            appeal_context="Frame obesity as a chronic disease per AMA 2013.",
        )
        MedicationContext.objects.create(
            drug_class="Anti-CGRP monoclonal antibody",
            regex=r"(aimovig|ajovy|emgality|vyepti)",
            appeal_context="Cite American Headache Society 2024 guidance.",
        )

    def test_returns_none_when_denial_text_unrelated(self):
        denial = Denial.objects.create(
            denial_text="Claim denied for routine wellness visit",
            hashed_email="x@example.com",
        )
        self.assertIsNone(AppealGenerator._collect_medication_context(denial))

    def test_matches_drug_in_denial_text(self):
        denial = Denial.objects.create(
            denial_text="We are denying your request for Ozempic.",
            hashed_email="x@example.com",
        )
        result = AppealGenerator._collect_medication_context(denial)
        self.assertIsNotNone(result)
        self.assertIn("GLP-1 receptor agonist", result)
        self.assertIn("AMA 2013", result)

    def test_matches_drug_mentioned_in_health_history(self):
        denial = Denial.objects.create(
            denial_text="Coverage denied",
            health_history="Patient has been on Aimovig for 6 months",
            hashed_email="x@example.com",
        )
        result = AppealGenerator._collect_medication_context(denial)
        self.assertIsNotNone(result)
        self.assertIn("Anti-CGRP", result)

    def test_includes_fda_indications_when_present(self):
        denial = Denial.objects.create(
            denial_text="Wegovy is not covered.",
            hashed_email="x@example.com",
        )
        result = AppealGenerator._collect_medication_context(denial)
        self.assertIn("FDA-approved indications:", result)
        self.assertIn("Type 2 diabetes", result)

    def test_omits_fda_indications_when_blank(self):
        denial = Denial.objects.create(
            denial_text="Vyepti was denied.",
            hashed_email="x@example.com",
        )
        result = AppealGenerator._collect_medication_context(denial)
        self.assertNotIn("FDA-approved indications:", result)

    def test_multiple_classes_concatenated(self):
        denial = Denial.objects.create(
            denial_text="Patient takes both Ozempic and Aimovig.",
            hashed_email="x@example.com",
        )
        result = AppealGenerator._collect_medication_context(denial)
        self.assertIn("GLP-1 receptor agonist", result)
        self.assertIn("Anti-CGRP", result)

    def test_inactive_contexts_excluded(self):
        MedicationContext.objects.filter(drug_class="GLP-1 receptor agonist").update(
            active=False
        )
        denial = Denial.objects.create(
            denial_text="Ozempic was denied.",
            hashed_email="x@example.com",
        )
        self.assertIsNone(AppealGenerator._collect_medication_context(denial))

    def test_returns_none_when_all_inputs_blank(self):
        denial = Denial.objects.create(denial_text="", hashed_email="x@example.com")
        self.assertIsNone(AppealGenerator._collect_medication_context(denial))


class MedicationContextPromptInjectionTests(TestCase):
    """Test that medication_context flows through make_open_prompt."""

    def test_make_open_prompt_includes_medication_context(self):
        gen = AppealGenerator()
        prompt = gen.make_open_prompt(
            denial_text="Denied",
            medication_context="Some curated guidance",
        )
        self.assertIn("DRUG-CLASS GUIDANCE", prompt)
        self.assertIn("Some curated guidance", prompt)

    def test_make_open_prompt_omits_block_when_no_context(self):
        gen = AppealGenerator()
        prompt = gen.make_open_prompt(denial_text="Denied")
        self.assertNotIn("DRUG-CLASS GUIDANCE", prompt)


class MedicationContextSeedDataTests(TestCase):
    """Verify the 0166 data migration produced the expected seed rows.

    No setUp cleanup here — these tests assert on the rows the migration
    creates against the test database.
    """

    EXPECTED_DRUG_CLASSES = {
        "GLP-1 receptor agonist (obesity / type 2 diabetes)",
        "Anti-CGRP monoclonal antibody (migraine prevention)",
        "TNF inhibitor (rheumatology / IBD / dermatology)",
        "IL-4/IL-13 inhibitor (atopic disease)",
        "Gender-affirming hormone therapy",
    }

    def test_all_expected_classes_present(self):
        actual = set(MedicationContext.objects.values_list("drug_class", flat=True))
        self.assertTrue(
            self.EXPECTED_DRUG_CLASSES.issubset(actual),
            f"Missing classes: {self.EXPECTED_DRUG_CLASSES - actual}",
        )

    def test_seed_rows_are_active_by_default(self):
        for drug_class in self.EXPECTED_DRUG_CLASSES:
            row = MedicationContext.objects.get(drug_class=drug_class)
            self.assertTrue(row.active, f"{drug_class} should be active")

    def test_glp1_seed_matches_ozempic(self):
        glp1 = MedicationContext.objects.get(
            drug_class="GLP-1 receptor agonist (obesity / type 2 diabetes)"
        )
        self.assertTrue(glp1.matches("Patient is on Ozempic"))
        self.assertTrue(glp1.matches("tirzepatide weekly"))

    def test_seed_collect_returns_curated_context_for_drug_mention(self):
        from fighthealthinsurance.models import Denial

        denial = Denial.objects.create(
            denial_text="We are denying coverage for Wegovy.",
            hashed_email="seed-test@example.com",
        )
        result = AppealGenerator._collect_medication_context(denial)
        self.assertIsNotNone(result)
        self.assertIn("GLP-1 receptor agonist", result)
