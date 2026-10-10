"""Jev's answers put to work: the letter's triage on the review and
questions pages, in an assistant's case, over REST and in the admin; the
research judgments on the fax page; and Jev's health on the staff pages.

A triage is written straight onto the row here (as extract_set_triage
would), so nothing in these tests asks TypeSafe anything.
"""

import datetime
from unittest.mock import patch

from asgiref.sync import async_to_sync
from bs4 import BeautifulSoup
from django import forms
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from fighthealthinsurance import models
from fighthealthinsurance.common_view_logic import (
    ChooseAppealHelper,
    DenialCreatorHelper,
    FindNextStepsHelper,
)
from fighthealthinsurance.form_utils import magic_combined_form
from fighthealthinsurance.ml import denial_triage as dt
from fighthealthinsurance.ml import research_judging as rj
from fighthealthinsurance.rest_serializers import DenialModelSerializer
from fighthealthinsurance.views import GenerateAppeal, add_pubmed_article_fields
from tests.sync.test_back_url_token import EMAIL
from tests.sync.test_review_page_prefill import HINT, ReviewPageTestBase, shown

TODAY = datetime.date(2026, 10, 3)


def triage(denial, **overrides):
    """Store a current, confident triage of the denial's letter."""
    values = dict(
        triage_category="medical_necessity",
        triage_category_confidence=0.9,
        triage_regulation="medicare_advantage",
        triage_regulation_confidence=0.9,
        triage_pre_service=0.95,
        triage_urgent=0.9,
        triage_source=dt.SOURCE,
        triage_text_hash=dt.text_hash(denial.denial_text),
        triaged_at=timezone.now(),
    )
    values.update(overrides)
    models.Denial.objects.filter(pk=denial.pk).update(**values)
    denial.refresh_from_db()
    return denial


def denial_type(name, form=None):
    return models.DenialTypes.objects.get_or_create(
        name=name, defaults={"regex": "zzz-never-matches", "form": form}
    )[0]


class ReviewPageSuggestionsTest(ReviewPageTestBase):
    def setUp(self):
        super().setUp()
        self.necessary = denial_type("Medically Necessary")
        self.pre_service = denial_type("Pre-Service")
        self.medicare_advantage = models.PlanSource.objects.create(
            name="Medicare Advantage"
        )

    def without_a_plan_source(self):
        self.denial.plan_source.clear()

    def test_the_suggested_denial_types_start_ticked_and_say_so(self):
        triage(self.denial)
        page = self.on_to_review()
        ticked = shown(page, "denial_type")
        self.assertIn(str(self.necessary.pk), ticked)
        self.assertIn(str(self.pre_service.pk), ticked)
        field = page.find(attrs={"name": "denial_type"})
        self.assertIn(HINT, str(field.find_parent("tr") or field.parent))

    def test_the_suggested_plan_source_fills_an_empty_one(self):
        self.without_a_plan_source()
        triage(self.denial)
        page = self.on_to_review()
        self.assertEqual(shown(page, "plan_source"), [str(self.medicare_advantage.pk)])

    def test_a_plan_source_on_the_row_is_kept(self):
        triage(self.denial)
        page = self.on_to_review()
        self.assertEqual(shown(page, "plan_source"), [str(self.plan_source.pk)])

    def test_the_way_back_shows_the_row_as_left(self):
        self.without_a_plan_source()
        triage(self.denial)
        page = self.back_to_review()
        self.assertEqual(shown(page, "denial_type"), [])
        self.assertEqual(shown(page, "plan_source"), [])

    def test_no_suggestion_without_consent(self):
        self.without_a_plan_source()
        triage(self.denial)
        models.Denial.objects.filter(pk=self.denial.pk).update(use_external=False)
        page = self.on_to_review()
        self.assertEqual(shown(page, "denial_type"), [])
        self.assertEqual(shown(page, "plan_source"), [])

    def test_no_suggestion_from_a_replaced_letter(self):
        self.without_a_plan_source()
        triage(self.denial, triage_text_hash=dt.text_hash("an older letter"))
        page = self.on_to_review()
        self.assertEqual(shown(page, "denial_type"), [])

    def test_the_deadline_shows_only_with_the_flag(self):
        triage(
            self.denial,
            appeal_deadline=TODAY + datetime.timedelta(days=60),
            appeal_deadline_label="180 days from notice",
            appeal_deadline_confidence=0.9,
        )
        self.assertNotIn("appears to say appeals are due", str(self.on_to_review()))
        with override_settings(TYPESAFE_DEADLINE_SHOW_ENABLED=True):
            page = str(self.on_to_review())
        self.assertIn("appears to say appeals are due by December 2, 2026", page)


class QuestionsPageSuggestionsTest(TestCase):
    def setUp(self):
        self.denial = models.Denial.objects.create(
            denial_text="We denied the MRI as not medically necessary.",
            hashed_email=models.Denial.get_hashed_email(EMAIL),
        )
        self.denial.denial_type.add(
            denial_type("Medically Necessary", form="MedicalNecessaryQuestions")
        )

    def combined(self, answers=None):
        question_forms = FindNextStepsHelper._build_question_forms(self.denial, answers)
        return question_forms, magic_combined_form(question_forms, answers or {})

    def test_the_urgent_and_pre_service_boxes_start_ticked_and_say_so(self):
        triage(self.denial)
        _forms, combined = self.combined()
        for name in ("urgent", "pre_service"):
            with self.subTest(box=name):
                self.assertIs(combined.fields[name].initial, True)
                self.assertIn(HINT, str(combined.fields[name].help_text))

    def test_a_stored_answer_wins(self):
        triage(self.denial)
        _forms, combined = self.combined({"urgent": "False"})
        self.assertIs(combined.fields["urgent"].initial, False)

    def test_nothing_is_ticked_without_a_triage(self):
        _forms, combined = self.combined()
        self.assertFalse(combined.fields["urgent"].initial)

    def test_an_unticked_suggestion_is_saved_as_an_answer(self):
        triage(self.denial)
        answers = GenerateAppeal._unticked_checkbox_answers(self.denial, {"pre_service"})
        self.assertEqual(answers.get("urgent"), "False")
        self.assertNotIn("pre_service", answers)


class AssistantCaseSuggestionsTest(TestCase):
    def setUp(self):
        self.denial = models.Denial.objects.create(
            denial_text="We denied the MRI as not medically necessary.",
            hashed_email="h",
        )
        self.necessary = denial_type("Medically Necessary")
        self.pre_service = denial_type("Pre-Service")
        self.medicare_advantage = models.PlanSource.objects.create(
            name="Medicare Advantage"
        )

    def apply(self):
        return async_to_sync(DenialCreatorHelper.apply_triage_suggestions)(
            self.denial.denial_id
        )

    def test_confident_suggestions_are_stored_with_their_source(self):
        triage(self.denial)
        self.assertEqual(self.apply(), 3)
        self.assertEqual(
            set(self.denial.denial_type.all()), {self.necessary, self.pre_service}
        )
        self.assertEqual(list(self.denial.plan_source.all()), [self.medicare_advantage])
        self.assertEqual(
            set(
                models.DenialTypesRelation.objects.filter(
                    denial=self.denial
                ).values_list("src__name", flat=True)
            ),
            {dt.TRIAGE_DATA_SOURCE},
        )

    def test_below_the_apply_bar_nothing_is_stored(self):
        triage(
            self.denial,
            triage_category_confidence=0.8,
            triage_regulation_confidence=0.8,
            triage_pre_service=0.8,
        )
        self.assertEqual(self.apply(), 0)
        self.assertFalse(self.denial.denial_type.exists())

    def test_what_the_case_already_has_is_not_added_twice(self):
        regex = models.DataSource.objects.get_or_create(name="regex")[0]
        models.DenialTypesRelation.objects.create(
            denial=self.denial, denial_type=self.necessary, src=regex
        )
        other = models.PlanSource.objects.create(name="Union")
        self.denial.plan_source.add(other)
        triage(self.denial)
        self.assertEqual(self.apply(), 1)
        self.assertEqual(
            models.DenialTypesRelation.objects.filter(
                denial=self.denial, denial_type=self.necessary
            ).count(),
            1,
        )
        self.assertEqual(list(self.denial.plan_source.all()), [other])

    def test_a_new_letter_takes_them_away(self):
        triage(self.denial)
        self.apply()
        DenialCreatorHelper._invalidate_denial_text_artifacts(self.denial)
        self.assertFalse(self.denial.denial_type.exists())
        self.assertFalse(self.denial.plan_source.exists())
        self.denial.refresh_from_db()
        self.assertIsNone(self.denial.triage_category)


class RestTriageTest(TestCase):
    def setUp(self):
        self.denial = models.Denial.objects.create(
            denial_text="We denied the MRI.", hashed_email="h"
        )

    def test_the_raw_columns_are_not_served(self):
        data = DenialModelSerializer(triage(self.denial)).data
        for column in dt.TRIAGE_COLUMNS:
            self.assertNotIn(column, data)

    def test_a_current_triage_is_served_without_the_deadline_by_default(self):
        triage(
            self.denial,
            appeal_deadline=timezone.localdate() + datetime.timedelta(days=60),
            appeal_deadline_confidence=0.9,
            appeal_deadline_label="180 days from notice",
        )
        data = DenialModelSerializer(self.denial).data["triage"]
        self.assertEqual(data["category"], "medical_necessity")
        self.assertIsNone(data["appeal_deadline"])
        with override_settings(TYPESAFE_DEADLINE_SHOW_ENABLED=True):
            data = DenialModelSerializer(self.denial).data["triage"]
        self.assertIsNotNone(data["appeal_deadline"])

    def test_a_stale_triage_is_not_served(self):
        triage(self.denial, triage_text_hash=dt.text_hash("an older letter"))
        self.assertIsNone(DenialModelSerializer(self.denial).data["triage"])


class DeadlineLabelAdminTest(TestCase):
    def setUp(self):
        admin = get_user_model().objects.create_superuser(
            username="admin", password="pw123", email="admin@example.com"
        )
        self.client.force_login(admin)
        self.denial = triage(
            models.Denial.objects.create(denial_text="Letter.", hashed_email="h"),
            appeal_deadline_label="180 days from notice",
            appeal_deadline_confidence=0.9,
        )
        self.untriaged = models.Denial.objects.create(
            denial_text="Other.", hashed_email="h2"
        )

    def test_only_a_current_triage_deadline_is_labelled(self):
        self.client.post(
            reverse("admin:fighthealthinsurance_denial_changelist"),
            {
                "action": "mark_deadline_wrong",
                "_selected_action": [self.denial.pk, self.untriaged.pk],
            },
        )
        self.denial.refresh_from_db()
        self.untriaged.refresh_from_db()
        self.assertEqual(self.denial.appeal_deadline_check, dt.DEADLINE_WRONG)
        self.assertIsNone(self.untriaged.appeal_deadline_check)


class FaxArticleOrderTest(TestCase):
    def setUp(self):
        self.denial = models.Denial.objects.create(
            denial_text="Letter.",
            hashed_email="h",
            procedure="MRI lumbar spine",
            diagnosis="low back pain",
            pubmed_ids_json=["1", "2", "3"],
        )
        for pmid in ("1", "2", "3"):
            models.PubMedArticleSummarized.objects.create(pmid=pmid, title=f"T{pmid}")
        key = rj.treatment_key(self.denial.procedure, self.denial.diagnosis)
        for pmid, on_topic, supports in (("1", 0.05, 0.0), ("3", 0.9, 0.95)):
            models.PubMedArticleJudgment.objects.create(
                pmid=pmid,
                treatment_key=key,
                on_topic=on_topic,
                supports=supports,
                undermines=0.0,
                scorer=rj.SCORER,
            )

    def test_supporting_first_and_off_topic_offered_unticked(self):
        articles = ChooseAppealHelper.candidate_articles(self.denial.denial_id, self.denial)
        self.assertEqual([a.pmid for a in articles], ["3", "2", "1"])

        class FaxForm(forms.Form):
            pass

        fax_form = FaxForm()
        add_pubmed_article_fields(fax_form, articles)
        self.assertTrue(fax_form.fields["pubmed_3"].initial)
        self.assertTrue(fax_form.fields["pubmed_2"].initial)
        self.assertFalse(fax_form.fields["pubmed_1"].initial)


class JevDashboardPanelTest(TestCase):
    def setUp(self):
        get_user_model().objects.create_user(
            username="staff", password="pw123", is_staff=True
        )
        self.client.login(username="staff", password="pw123")

    def panel(self):
        response = self.client.get(reverse("model_usage_dashboard"))
        self.assertEqual(response.status_code, 200)
        return response, response.context["jev"]

    def row(self, panel, service):
        return next(r for r in panel["rows"] if r["service"] == service)

    def test_every_use_has_a_row_and_is_off_by_default(self):
        response, panel = self.panel()
        services = {r["service"] for r in panel["rows"]}
        self.assertEqual(
            services,
            {
                "typesafe",
                dt.SERVICE,
                "typesafe-chat-gate",
                "typesafe-chat",
                rj.SERVICE,
            },
        )
        self.assertTrue(all(r["level"] == "off" for r in panel["rows"]))
        self.assertContains(response, "Jev (TypeSafe)")

    def test_a_fresh_failure_reads_as_failing_with_its_hint(self):
        models.ExternalServiceHealth.objects.create(
            service=dt.SERVICE,
            last_success_at=timezone.now() - datetime.timedelta(hours=2),
            last_failure_at=timezone.now(),
            last_failure="HTTP 402",
        )
        with self.settings(TYPESAFE_API_KEY="k", TYPESAFE_DENIAL_TRIAGE_ENABLED=True):
            response, panel = self.panel()
        row = self.row(panel, dt.SERVICE)
        self.assertEqual(row["level"], "failing")
        self.assertIn("payment required", row["last_failure_hint"])
        self.assertContains(response, "payment required")

    def test_a_recent_answer_reads_as_ok(self):
        models.ExternalServiceHealth.objects.create(
            service=dt.SERVICE, last_success_at=timezone.now()
        )
        with self.settings(TYPESAFE_API_KEY="k", TYPESAFE_DENIAL_TRIAGE_ENABLED=True):
            _response, panel = self.panel()
        self.assertEqual(self.row(panel, dt.SERVICE)["level"], "ok")

    def test_an_old_answer_reads_as_idle(self):
        models.ExternalServiceHealth.objects.create(
            service=dt.SERVICE, last_success_at=timezone.now() - datetime.timedelta(days=2)
        )
        with self.settings(TYPESAFE_API_KEY="k", TYPESAFE_DENIAL_TRIAGE_ENABLED=True):
            _response, panel = self.panel()
        self.assertEqual(self.row(panel, dt.SERVICE)["level"], "idle")

    def test_the_deadline_tally_and_the_probe(self):
        denial = models.Denial.objects.create(denial_text="Letter.", hashed_email="h")
        triage(
            denial,
            appeal_deadline_label="180 days from notice",
            appeal_deadline_confidence=0.9,
            appeal_deadline_check=dt.DEADLINE_CORRECT,
        )
        models.ModelBackendHealthCheckResult.objects.create(
            run_id="r",
            provider="typesafe",
            model_name="jev-1.13.0",
            category="FAIL_AUTH",
            error="HTTP 401",
        )
        response, panel = self.panel()
        self.assertEqual(panel["deadlines"]["correct"], 1)
        self.assertEqual(panel["deadlines"]["wrong"], 0)
        self.assertEqual(panel["deadlines"]["unlabelled"], 0)
        self.assertEqual(panel["probe"].category, "FAIL_AUTH")
        self.assertContains(response, "FAIL_AUTH")


class ModelBackendStatusTypeSafeRowTest(TestCase):
    def test_the_status_page_lists_typesafe(self):
        get_user_model().objects.create_user(
            username="staff", password="pw123", is_staff=True
        )
        self.client.login(username="staff", password="pw123")
        response = self.client.get(reverse("model_backend_status"))
        self.assertEqual(response.status_code, 200)
        row = next(r for r in response.context["rows"] if r["provider"] == "typesafe")
        self.assertEqual(row["config_category"], "NOT_CONFIGURED")
        self.assertFalse(row["enabled"])
