"""Our model reads a shared plan document's insurer, plan year and plan type
in the same reading as its sponsor, and reads more of a long document for
the employer.

A clean file once stated its insurer and its 2026 coverage period on page
one, and both stayed blank on its row. Now the model's one reading gives
them too, each checked against the text (the checks themselves are in
tests/async-unit/test_plan_sponsor_reader_facts.py). They fill only the
facts the row has empty, marked as the model's: never a value the person
gave, or one staff set or cleared. When the model finds no sponsor in a
document longer than its excerpt, it reads the passages the excerpt left
out once more, inside the same time limit. link_plan_sponsors does the same
for documents read before, listing counts unless told to apply them.
"""

import asyncio
import json
import time
import uuid
from contextlib import contextmanager
from io import StringIO
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import RequestFactory, TestCase
from django.utils import timezone

from fighthealthinsurance.admin import PolicyDocumentAdmin
from fighthealthinsurance.models import PlanSponsor, PolicyDocument
from fighthealthinsurance.plan_documents_processing import (
    alink_company,
    aprocess_document,
    clean,
)
from fighthealthinsurance.plan_sponsor_reader import PlanFacts, PlanReading, aread_plan
from fighthealthinsurance.plan_sponsors import Sponsor
from tests.plan_text_samples import long_spd

PAGE_ONE = (
    "Summary of Benefits and Coverage: What this Plan Covers & What You Pay\n"
    "Crumpler Health Mutual: Gold PPO    Coverage Period: 01/01/2026 - 12/31/2026\n"
    "This group health plan is subject to ERISA.\n"
) + ("Covered services and what the plan pays. You pay less in network. " * 20)

FACTS = {
    "insurer": "Crumpler Health Mutual",
    "insurer_evidence": "Crumpler Health Mutual: Gold PPO",
    "plan_year": 2026,
    "plan_year_evidence": "Coverage Period: 01/01/2026 - 12/31/2026",
    "plan_type": "employer_erisa",
    "plan_type_evidence": "This group health plan is subject to ERISA.",
}
SPONSOR = {
    "sponsor": "Crumpler Cooperative",
    "evidence": "Plan Sponsor: Crumpler Cooperative",
}


def answer(**fields):
    return json.dumps({"sponsor": None, "ein": None, "evidence": None} | fields)


NO_SPONSOR = answer()


@contextmanager
def models_say(answer_for):
    """Our internal models, faked: ``answer_for(prompt)`` is their answer to
    each question, None for none. Yields the questions asked."""
    asked: list[str] = []

    async def fake(system_prompts, prompt, validator=None, **kwargs):
        asked.append(prompt)
        said = answer_for(prompt)
        if said is None or (validator is not None and not validator(said)):
            return None
        return said

    with patch("fighthealthinsurance.ml.ml_inference.infer_with_fallback", fake):
        yield asked


def read(text):
    return async_to_sync(aread_plan)(text, clean(text))


def shared_row(text=PAGE_ONE, **fields):
    return PolicyDocument.objects.create(
        source=PolicyDocument.SOURCE_SHARED,
        share_batch=uuid.uuid4(),
        shared_at=timezone.now(),
        fingerprint=uuid.uuid4().hex,
        processing_status=PolicyDocument.PROCESSING_READY,
        cleaned_text=text,
        **fields,
    )


def link(doc, said):
    with models_say(lambda prompt: said):
        async_to_sync(alink_company)(str(doc.pk), doc.cleaned_text, doc.cleaned_text)
    doc.refresh_from_db()
    return doc


def plan_facts(doc):
    return (doc.insurer_name, doc.plan_year, doc.plan_category)


def sources(doc):
    return (doc.insurer_name_source, doc.plan_year_source, doc.plan_category_source)


class TheReadingFillsThePlanFactsTest(TestCase):
    def test_the_insurer_plan_year_and_plan_type_are_filled(self):
        doc = link(shared_row(), answer(**FACTS))
        self.assertEqual(
            plan_facts(doc), ("Crumpler Health Mutual", 2026, "employer_erisa")
        )

    def test_each_fact_filled_is_marked_as_our_models(self):
        doc = link(shared_row(), answer(**FACTS))
        self.assertEqual(sources(doc), ("model", "model", "model"))

    def test_facts_that_fail_their_checks_are_not_stored(self):
        doc = link(
            shared_row(),
            answer(
                insurer="Fraser Mills Health",
                insurer_evidence="Insured by Fraser Mills Health",
                plan_year=2031,
                plan_year_evidence="Coverage Period: 01/01/2026 - 12/31/2026",
                plan_type="tricare",
                plan_type_evidence="This group health plan is subject to ERISA.",
            ),
        )
        self.assertEqual(
            (plan_facts(doc), sources(doc)), (("", None, "unknown"), ("", "", ""))
        )

    def test_values_the_person_gave_are_kept(self):
        doc = link(
            shared_row(
                insurer_name="Mahoney Mutual",
                plan_year=2025,
                plan_category="aca_marketplace",
            ),
            answer(**FACTS),
        )
        self.assertEqual(
            (plan_facts(doc), sources(doc)),
            (("Mahoney Mutual", 2025, "aca_marketplace"), ("", "", "")),
        )

    def test_a_plan_type_the_person_was_not_sure_of_is_filled(self):
        doc = link(shared_row(plan_category="unknown"), answer(**FACTS))
        self.assertEqual(doc.plan_category, "employer_erisa")

    def test_facts_staff_set_or_cleared_are_not_filled(self):
        doc = link(
            shared_row(
                insurer_name="",
                insurer_name_source="staff",
                plan_year=2024,
                plan_year_source="staff",
                plan_category="unknown",
                plan_category_source="staff",
            ),
            answer(**FACTS),
        )
        self.assertEqual(plan_facts(doc), ("", 2024, "unknown"))

    def test_facts_come_with_an_answer_that_names_no_sponsor(self):
        doc = link(shared_row(), answer(**FACTS))
        self.assertIsNone(doc.plan_sponsor_id)
        self.assertEqual(doc.insurer_name, "Crumpler Health Mutual")

    def test_facts_come_with_the_sponsor_in_the_same_answer(self):
        text = "Plan Sponsor: Crumpler Cooperative\n" + PAGE_ONE
        doc = link(shared_row(text), answer(**SPONSOR, **FACTS))
        self.assertEqual(
            (doc.plan_sponsor.name, doc.insurer_name, doc.plan_year),
            ("Crumpler Cooperative", "Crumpler Health Mutual", 2026),
        )

    def test_a_row_removed_while_it_was_read_gets_no_facts(self):
        doc = shared_row()
        PolicyDocument.objects.filter(pk=doc.pk).update(
            processing_status=PolicyDocument.PROCESSING_REMOVED, cleaned_text=""
        )
        with models_say(lambda prompt: answer(**FACTS)):
            async_to_sync(alink_company)(str(doc.pk), PAGE_ONE, clean(PAGE_ONE))
        doc.refresh_from_db()
        self.assertEqual(plan_facts(doc), ("", None, "unknown"))

    def test_a_shared_file_gets_the_insurer_and_year_on_its_first_page(self):
        doc = PolicyDocument.objects.create(
            source=PolicyDocument.SOURCE_SHARED,
            share_batch=uuid.uuid4(),
            shared_at=timezone.now(),
            processing_status=PolicyDocument.PROCESSING_RECEIVED,
            filename="sbc.txt",
        )
        with patch(
            "fighthealthinsurance.plan_documents_processing._read_stored",
            return_value=PAGE_ONE.encode(),
        ), models_say(lambda prompt: answer(**FACTS)):
            async_to_sync(aprocess_document)(str(doc.pk))
        doc.refresh_from_db()
        self.assertEqual(
            (doc.processing_status, doc.insurer_name, doc.plan_year),
            (PolicyDocument.PROCESSING_READY, "Crumpler Health Mutual", 2026),
        )

    def test_a_member_or_birth_date_quoted_without_its_label_is_never_a_fact(self):
        doc = PolicyDocument.objects.create(
            source=PolicyDocument.SOURCE_SHARED,
            share_batch=uuid.uuid4(),
            shared_at=timezone.now(),
            processing_status=PolicyDocument.PROCESSING_RECEIVED,
            filename="sbc.txt",
        )
        member = "Member Name: Rebecca Lee Crumpler    Date of Birth: 03/14/2001\n"
        with patch(
            "fighthealthinsurance.plan_documents_processing._read_stored",
            return_value=(PAGE_ONE + member).encode(),
        ), models_say(
            lambda prompt: answer(
                insurer="Rebecca Lee Crumpler",
                insurer_evidence="Rebecca Lee Crumpler",
                plan_year=2001,
                plan_year_evidence="03/14/2001",
            )
        ):
            async_to_sync(aprocess_document)(str(doc.pk))
        doc.refresh_from_db()
        self.assertEqual(
            (doc.processing_status, doc.insurer_name, doc.plan_year),
            (PolicyDocument.PROCESSING_READY, "", None),
        )


def sponsor_in_plan_information(prompt):
    if "Plan Sponsor: Crumpler Cooperative" in prompt:
        return answer(**SPONSOR)
    return NO_SPONSOR


class ASecondLookAtALongDocumentTest(TestCase):
    def test_a_sponsor_in_the_part_the_excerpt_left_out_is_found(self):
        with models_say(sponsor_in_plan_information) as asked:
            reading = read(long_spd())
        self.assertEqual(
            (reading.sponsor.name, reading.answered, len(asked)),
            ("Crumpler Cooperative", True, 2),
        )

    def test_a_sponsor_found_the_second_time_is_not_kept_as_the_insurer(self):
        text = "Administered by Crumpler Cooperative\n" + long_spd()

        def answer_for(prompt):
            if "Plan Sponsor: Crumpler Cooperative" in prompt:
                return answer(**SPONSOR)
            return answer(
                insurer="Crumpler Cooperative",
                insurer_evidence="Administered by Crumpler Cooperative",
            )

        with models_say(answer_for):
            reading = read(text)
        self.assertEqual(
            (reading.sponsor.name, reading.facts.insurer_name),
            ("Crumpler Cooperative", ""),
        )

    def test_the_sponsor_as_checked_is_not_kept_as_the_insurer(self):
        text = (
            "Plan Sponsor: Crumpler Cooperative Medical Plan\n"
            "Administered by Crumpler Cooperative\n" + PAGE_ONE
        )
        with models_say(
            lambda prompt: answer(
                sponsor="Crumpler Cooperative Medical Plan",
                evidence="Plan Sponsor: Crumpler Cooperative Medical Plan",
                insurer="Crumpler Cooperative",
                insurer_evidence="Administered by Crumpler Cooperative",
            )
        ):
            reading = read(text)
        self.assertEqual(
            (reading.sponsor.name, reading.facts.insurer_name),
            ("Crumpler Cooperative", ""),
        )

    def test_it_is_asked_at_most_once(self):
        with models_say(lambda prompt: NO_SPONSOR) as asked:
            reading = read(long_spd())
        self.assertEqual(
            (reading.sponsor, reading.answered, len(asked)), (None, True, 2)
        )

    def test_a_document_the_excerpt_reads_whole_is_read_once(self):
        with models_say(lambda prompt: NO_SPONSOR) as asked:
            read(PAGE_ONE)
        self.assertEqual(len(asked), 1)

    def test_a_sponsor_found_the_first_time_is_not_read_again(self):
        text = "Plan Sponsor: Crumpler Cooperative\n" + long_spd()
        with models_say(lambda prompt: answer(**SPONSOR)) as asked:
            reading = read(text)
        self.assertEqual(
            (reading.sponsor.name, len(asked)), ("Crumpler Cooperative", 1)
        )

    def test_with_no_usable_answer_it_is_not_read_again(self):
        with models_say(lambda prompt: None) as asked:
            reading = read(long_spd())
        self.assertEqual((reading.answered, len(asked)), (False, 1))

    def test_it_is_not_asked_without_a_models_time_left(self):
        with models_say(lambda prompt: NO_SPONSOR) as asked, patch(
            "fighthealthinsurance.plan_sponsor_reader.TOTAL_TIMEOUT_SECONDS", 20.0
        ):
            read(long_spd())
        self.assertEqual(len(asked), 1)

    def test_it_ends_with_the_first_readings_time_limit(self):
        asked: list[str] = []

        async def slow_the_second_time(system_prompts, prompt, validator=None, **kw):
            asked.append(prompt)
            if len(asked) > 1:
                await asyncio.sleep(5)
            return NO_SPONSOR if validator(NO_SPONSOR) else None

        started = time.monotonic()
        with patch(
            "fighthealthinsurance.ml.ml_inference.infer_with_fallback",
            slow_the_second_time,
        ), patch(
            "fighthealthinsurance.plan_sponsor_reader.TOTAL_TIMEOUT_SECONDS", 0.5
        ), patch(
            "fighthealthinsurance.plan_sponsor_reader.MODEL_TIMEOUT_SECONDS", 0.1
        ), patch(
            "fighthealthinsurance.plan_sponsor_reader.logger.warning"
        ):
            reading = read(long_spd())
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(
            (reading.sponsor, reading.answered, len(asked)), (None, True, 2)
        )

    def test_the_facts_come_from_the_first_reading(self):
        def facts_only_the_second_time(prompt):
            if "Plan Sponsor: Crumpler Cooperative" in prompt:
                return answer(**SPONSOR, **FACTS)
            return NO_SPONSOR

        with models_say(facts_only_the_second_time):
            reading = read(long_spd() + PAGE_ONE)
        self.assertEqual(reading.facts, PlanFacts())


class StaffChangingAFactTest(TestCase):
    def test_a_fact_staff_change_is_marked_as_theirs(self):
        staff = get_user_model().objects.create_superuser("staff", "s@example.com", "x")
        request = RequestFactory().post("/")
        request.user = staff
        doc = link(shared_row(), answer(**FACTS))

        class Form:
            changed_data = ["insurer_name"]

        doc.insurer_name = "Crumpler Health Mutual of Ohio"
        PolicyDocumentAdmin(PolicyDocument, AdminSite()).save_model(
            request, doc, Form(), change=True
        )
        doc.refresh_from_db()
        self.assertEqual(sources(doc), ("staff", "model", "model"))


READ = PlanReading(
    None, True, PlanFacts("Crumpler Health Mutual", 2026, "employer_erisa")
)


class FillingFactsForDocumentsReadBeforeTest(TestCase):
    def setUp(self):
        self.reader = AsyncMock(return_value=READ)
        reader = patch(
            "fighthealthinsurance.plan_sponsor_reader.aread_plan", self.reader
        )
        reader.start()
        self.addCleanup(reader.stop)

    def run_command(self, *args):
        out = StringIO()
        call_command("link_plan_sponsors", *args, stdout=out)
        return out.getvalue()

    def test_the_list_counts_the_facts_it_would_fill(self):
        shared_row()
        self.assertIn(
            "would be filled: insurer 1, plan year 1, plan type 1", self.run_command()
        )

    def test_the_list_writes_nothing(self):
        doc = shared_row()
        self.run_command()
        doc.refresh_from_db()
        self.assertEqual(
            (plan_facts(doc), sources(doc)), (("", None, "unknown"), ("", "", ""))
        )

    def test_the_list_shows_no_text_and_no_fact(self):
        shared_row()
        out = self.run_command()
        for read_from_the_file in ("Crumpler Health Mutual", "Coverage Period", "2026"):
            with self.subTest(read_from_the_file):
                self.assertNotIn(read_from_the_file, out)

    def test_the_list_does_not_count_a_fact_the_person_gave(self):
        shared_row(plan_year=2025)
        self.assertIn("insurer 1, plan year 0, plan type 1", self.run_command())

    def test_apply_fills_only_the_empty_facts(self):
        doc = shared_row(plan_year=2025)
        self.run_command("--apply")
        doc.refresh_from_db()
        self.assertEqual(
            (plan_facts(doc), sources(doc)),
            (
                ("Crumpler Health Mutual", 2025, "employer_erisa"),
                ("model", "", "model"),
            ),
        )

    def test_a_row_with_its_company_and_every_fact_is_not_read(self):
        company = PlanSponsor.objects.create(
            name="Crumpler Cooperative", normalized_name="crumpler cooperative"
        )
        shared_row(
            plan_sponsor=company,
            plan_sponsor_source="staff",
            insurer_name="Mahoney Mutual",
            plan_year=2025,
            plan_category="aca_marketplace",
        )
        out = self.run_command("--apply")
        self.assertEqual(
            ("0 document(s) looked at" in out, self.reader.await_count), (True, 0)
        )

    def test_a_row_with_a_company_gets_its_facts_and_keeps_its_company(self):
        company = PlanSponsor.objects.create(
            name="Crumpler Cooperative", normalized_name="crumpler cooperative"
        )
        doc = shared_row(plan_sponsor=company, plan_sponsor_source="staff")
        self.reader.return_value = READ._replace(
            sponsor=Sponsor("Fraser Mills", "", source="model")
        )
        out = self.run_command("--apply")
        doc.refresh_from_db()
        self.assertEqual(
            (doc.plan_sponsor, doc.insurer_name, "0 linked" in out),
            (company, "Crumpler Health Mutual", True),
        )
