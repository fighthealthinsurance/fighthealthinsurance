"""The share page's goal counts companies, each once.

A plan through work names its sponsor, the employer, and a Summary Plan
Description gives the sponsor's Employer Identification Number. The reading
finds the company in the document's text (plan_sponsors.identify_sponsor),
looks it up on the running company list by EIN and then by name, adds it if
it's new, and links the document to it. The goal counts the companies on the
list that a counted document names: a company shared twice counts once, a
document that names no company counts for nothing toward the goal, and an
insurer's own policy names no employer. Sharers never have to name a
company. link_plan_sponsors does the same for documents read before.
"""

import datetime
import uuid
from io import StringIO

from asgiref.sync import async_to_sync
from django.core.management import call_command
from django.test import TestCase

from fighthealthinsurance.models import InsuranceCompany, PlanSponsor, PolicyDocument
from fighthealthinsurance.plan_documents_processing import aprocess_document
from fighthealthinsurance.plan_sponsors import (
    Sponsor,
    identify_sponsor,
    link_sponsor,
    normalize_name,
)
from fighthealthinsurance.shared_plan_documents import (
    distinct_plan_count,
    employer_plan_count,
    plan_shares,
)

START = datetime.datetime(2026, 10, 20, 15, 0, tzinfo=datetime.timezone.utc)

SPD_WITH_EIN = """Crumpler Cooperative Welfare Benefit Plan
Summary Plan Description, effective January 1, 2027

Plan Information
Name of Plan: Crumpler Cooperative Welfare Benefit Plan
Plan Sponsor: Crumpler Cooperative, LLC
Employer Identification Number (EIN): 12-3456789
Plan Number: 501
Plan Administrator: Benefits Committee
This plan is governed by ERISA.
""" + ("Covered services and what the plan pays. " * 20)

SPD_NAME_ONLY = """Mahoney Shipyards Health and Welfare Plan
Summary Plan Description

Your rights under the Employee Retirement Income Security Act (ERISA).
""" + ("Deductibles, copays and coinsurance for each service. " * 20)

INSURER_POLICY = """Crumpler Health Mutual Medical Policy
Occipital Nerve Block
Policy number MED.00123. Effective date: 03/01/2026

This medical policy describes when occipital nerve blocks are medically
necessary. Benefits are subject to the member's contract.
""" + ("Clinical criteria and references for the procedure. " * 20)

INSURER_AS_SPONSOR = """Summary Plan Description
Plan Sponsor: Crumpler Health Mutual
This group health plan is governed by ERISA.
""" + ("Covered services. " * 40)


class FindingTheCompanyTest(TestCase):
    def test_the_sponsor_and_its_ein_are_read_from_an_spd(self):
        self.assertEqual(
            identify_sponsor(SPD_WITH_EIN),
            Sponsor(name="Crumpler Cooperative, LLC", ein="12-3456789"),
        )

    def test_a_plan_title_names_the_company_when_no_sponsor_line_does(self):
        self.assertEqual(
            identify_sponsor(SPD_NAME_ONLY),
            Sponsor(name="Mahoney Shipyards", ein=""),
        )

    def test_an_insurers_medical_policy_names_no_company(self):
        self.assertIsNone(identify_sponsor(INSURER_POLICY))

    def test_an_insurer_named_as_sponsor_is_not_a_company(self):
        InsuranceCompany.objects.create(name="Crumpler Health Mutual")
        self.assertIsNone(identify_sponsor(INSURER_AS_SPONSOR))

    def test_a_line_of_test_text_names_no_company(self):
        self.assertIsNone(identify_sponsor("g"))

    def test_a_whole_header_is_not_taken_for_the_name(self):
        text = (
            "Summary Plan Description\nPlan Sponsor Name Address\n"
            "Crumpler Cooperative\n12 Mill Road, Crumpler, NC\n"
        ) + ("Covered services. " * 40)
        self.assertEqual(identify_sponsor(text).name, "Crumpler Cooperative")

    def test_an_ein_far_from_the_sponsor_is_not_taken_as_its_own(self):
        text = (
            "Summary Plan Description\nPlan Sponsor: Crumpler Cooperative\n"
            + ("Covered services and what the plan pays. " * 40)
            + "\nClaims administrator Fraser Mills Benefits, "
            "Employer Identification Number 98-7654321\n"
        )
        self.assertEqual(
            identify_sponsor(text), Sponsor(name="Crumpler Cooperative", ein="")
        )

    def test_names_match_without_punctuation_or_legal_form(self):
        self.assertEqual(
            normalize_name("The Crumpler Cooperative, L.L.C."),
            normalize_name("crumpler cooperative"),
        )


def shared_row(text, at=None, fingerprint=None):
    return PolicyDocument.objects.create(
        source=PolicyDocument.SOURCE_SHARED,
        share_batch=uuid.uuid4(),
        shared_at=at or START,
        fingerprint=fingerprint or uuid.uuid4().hex,
        processing_status=PolicyDocument.PROCESSING_READY,
        cleaned_text=text,
    )


def linked(text, at=None):
    doc = shared_row(text, at=at)
    sponsor = identify_sponsor(text)
    if sponsor is not None:
        link_sponsor(doc.pk, sponsor)
    return doc


class TheCompanyListTest(TestCase):
    def test_a_new_company_is_added_and_linked(self):
        doc = linked(SPD_WITH_EIN)
        doc.refresh_from_db()
        self.assertEqual(doc.plan_sponsor.ein, "12-3456789")

    def test_the_same_ein_under_another_name_is_the_same_company(self):
        linked(SPD_WITH_EIN)
        linked(SPD_WITH_EIN.replace("Crumpler Cooperative, LLC", "Crumpler Co-op"))
        self.assertEqual(PlanSponsor.objects.count(), 1)

    def test_the_same_name_is_the_same_company(self):
        linked(SPD_NAME_ONLY)
        linked(SPD_NAME_ONLY)
        self.assertEqual(PlanSponsor.objects.count(), 1)

    def test_two_employers_with_one_name_but_two_eins_stay_apart(self):
        linked(SPD_WITH_EIN)
        linked(SPD_WITH_EIN.replace("12-3456789", "98-7654321"))
        self.assertEqual(PlanSponsor.objects.count(), 2)

    def test_a_name_first_seen_without_an_ein_takes_the_ein_later(self):
        linked(SPD_WITH_EIN.replace("Employer Identification Number (EIN): 12-3456789\n", ""))
        linked(SPD_WITH_EIN)
        self.assertEqual(
            list(PlanSponsor.objects.values_list("ein", flat=True)), ["12-3456789"]
        )

    def test_a_document_staff_linked_keeps_its_company(self):
        mine = PlanSponsor.objects.create(name="Fraser Mills", normalized_name="fraser mills")
        doc = shared_row(SPD_WITH_EIN)
        PolicyDocument.objects.filter(pk=doc.pk).update(plan_sponsor=mine)
        link_sponsor(doc.pk, identify_sponsor(SPD_WITH_EIN))
        doc.refresh_from_db()
        self.assertEqual(doc.plan_sponsor, mine)


class TheGoalCountsCompaniesTest(TestCase):
    def test_each_company_counts_once(self):
        linked(SPD_WITH_EIN)
        linked(SPD_WITH_EIN, at=START + datetime.timedelta(days=1))
        linked(SPD_NAME_ONLY, at=START + datetime.timedelta(days=2))
        self.assertEqual(employer_plan_count(), 2)

    def test_a_document_that_names_no_company_does_not_count_toward_the_goal(self):
        linked(INSURER_POLICY)
        self.assertEqual((distinct_plan_count(), employer_plan_count()), (1, 0))

    def test_the_share_that_brings_a_company_in_is_the_one_counted(self):
        linked(SPD_WITH_EIN)
        linked(SPD_WITH_EIN, at=START + datetime.timedelta(days=1))
        self.assertEqual([share.new_companies for share in plan_shares()], [1, 0])

    def test_potential_spam_naming_a_company_does_not_count(self):
        doc = linked(SPD_WITH_EIN)
        PolicyDocument.objects.filter(pk=doc.pk).update(
            processing_status=PolicyDocument.PROCESSING_SPAM
        )
        self.assertEqual(employer_plan_count(), 0)


class TheReadingLinksTheCompanyTest(TestCase):
    def test_a_shared_text_file_is_linked_when_read(self):
        doc = PolicyDocument.objects.create(
            source=PolicyDocument.SOURCE_SHARED,
            share_batch=uuid.uuid4(),
            shared_at=START,
            processing_status=PolicyDocument.PROCESSING_RECEIVED,
            filename="spd.txt",
        )
        from unittest.mock import patch

        with patch(
            "fighthealthinsurance.plan_documents_processing._read_stored",
            return_value=SPD_WITH_EIN.encode(),
        ):
            async_to_sync(aprocess_document)(str(doc.pk))
        doc.refresh_from_db()
        self.assertEqual(doc.plan_sponsor.name, "Crumpler Cooperative, LLC")


class LinkingDocumentsReadBeforeTest(TestCase):
    def test_it_lists_without_apply(self):
        shared_row(SPD_WITH_EIN)
        out = StringIO()
        call_command("link_plan_sponsors", stdout=out)
        self.assertEqual(
            (PlanSponsor.objects.count(), "1 would be linked" in out.getvalue()),
            (0, True),
        )

    def test_the_list_counts_one_ein_under_two_names_as_one_company(self):
        shared_row(SPD_WITH_EIN)
        shared_row(SPD_WITH_EIN.replace("Crumpler Cooperative, LLC", "Crumpler Co-op"))
        out = StringIO()
        call_command("link_plan_sponsors", stdout=out)
        self.assertIn("2 would be linked to 1 company", out.getvalue())

    def test_it_links_with_apply(self):
        doc = shared_row(SPD_WITH_EIN)
        call_command("link_plan_sponsors", "--apply", stdout=StringIO())
        doc.refresh_from_db()
        self.assertEqual(doc.plan_sponsor.ein, "12-3456789")


class TheCompanyFormTest(TestCase):
    def test_a_second_company_by_a_name_already_listed_without_an_ein_is_refused(self):
        from fighthealthinsurance.admin import PlanSponsorForm

        PlanSponsor.objects.create(name="Fraser Mills", normalized_name="fraser mills")
        form = PlanSponsorForm(data={"name": "Fraser Mills, Inc.", "ein": ""})
        self.assertIn("name", form.errors)

    def test_the_same_name_with_its_own_ein_is_taken(self):
        from fighthealthinsurance.admin import PlanSponsorForm

        PlanSponsor.objects.create(name="Fraser Mills", normalized_name="fraser mills")
        form = PlanSponsorForm(data={"name": "Fraser Mills", "ein": "98-7654321"})
        self.assertTrue(form.is_valid(), form.errors)
