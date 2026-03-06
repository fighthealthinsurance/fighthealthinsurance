"""Tests for charts export views: env var gating, staff-only access, and content."""

import json
import os
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from fighthealthinsurance.models import (
    Appeal,
    ChooserCandidate,
    ChooserTask,
    ChooserVote,
    Denial,
    DenialQA,
    GenericQuestionGeneration,
    OngoingChat,
    ProposedAppeal,
    PubMedArticleSummarized,
)

User = get_user_model()


class ExportEnvVarGatingTest(TestCase):
    """Test that export views return 403 when EXPORT_ENABLED is not set."""

    def setUp(self):
        self.staff_user = User.objects.create_user(
            username="staffuser", password="testpass123", is_staff=True
        )
        self.client.login(username="staffuser", password="testpass123")

    def test_de_identified_export_blocked_without_env(self):
        with patch.dict(os.environ, {}, clear=True):
            response = self.client.get(reverse("charts:de_identified_export"))
        self.assertEqual(response.status_code, 403)

    def test_chooser_export_blocked_without_env(self):
        with patch.dict(os.environ, {}, clear=True):
            response = self.client.get(reverse("charts:chooser_ranked_export"))
        self.assertEqual(response.status_code, 403)

    def test_denial_appeal_export_blocked_without_env(self):
        with patch.dict(os.environ, {}, clear=True):
            response = self.client.get(reverse("charts:denial_appeal_export"))
        self.assertEqual(response.status_code, 403)

    def test_chat_export_blocked_without_env(self):
        with patch.dict(os.environ, {}, clear=True):
            response = self.client.get(reverse("charts:chat_export"))
        self.assertEqual(response.status_code, 403)

    def test_questions_by_procedure_blocked_without_env(self):
        with patch.dict(os.environ, {}, clear=True):
            response = self.client.get(reverse("charts:questions_by_procedure_export"))
        self.assertEqual(response.status_code, 403)

    def test_denial_questions_blocked_without_env(self):
        with patch.dict(os.environ, {}, clear=True):
            response = self.client.get(reverse("charts:denial_questions_export"))
        self.assertEqual(response.status_code, 403)

    def test_pubmed_article_blocked_without_env(self):
        with patch.dict(os.environ, {}, clear=True):
            response = self.client.get(reverse("charts:pubmed_article_export"))
        self.assertEqual(response.status_code, 403)

    def test_chooser_export_allowed_with_env(self):
        with patch.dict(os.environ, {"EXPORT_ENABLED": "1"}):
            response = self.client.get(reverse("charts:chooser_ranked_export"))
        self.assertEqual(response.status_code, 200)

    def test_denial_appeal_export_allowed_with_env(self):
        with patch.dict(os.environ, {"EXPORT_ENABLED": "1"}):
            response = self.client.get(reverse("charts:denial_appeal_export"))
        self.assertEqual(response.status_code, 200)

    def test_chat_export_allowed_with_env(self):
        with patch.dict(os.environ, {"EXPORT_ENABLED": "1"}):
            response = self.client.get(reverse("charts:chat_export"))
        self.assertEqual(response.status_code, 200)

    def test_questions_by_procedure_allowed_with_env(self):
        with patch.dict(os.environ, {"EXPORT_ENABLED": "1"}):
            response = self.client.get(reverse("charts:questions_by_procedure_export"))
        self.assertEqual(response.status_code, 200)

    def test_denial_questions_allowed_with_env(self):
        with patch.dict(os.environ, {"EXPORT_ENABLED": "1"}):
            response = self.client.get(reverse("charts:denial_questions_export"))
        self.assertEqual(response.status_code, 200)

    def test_pubmed_article_allowed_with_env(self):
        with patch.dict(os.environ, {"EXPORT_ENABLED": "1"}):
            response = self.client.get(reverse("charts:pubmed_article_export"))
        self.assertEqual(response.status_code, 200)


class ExportStaffOnlyTest(TestCase):
    """Test that export views require staff access."""

    def setUp(self):
        self.regular_user = User.objects.create_user(
            username="regularuser", password="testpass123", is_staff=False
        )

    def test_exports_require_staff(self):
        self.client.login(username="regularuser", password="testpass123")
        urls = [
            reverse("charts:de_identified_export"),
            reverse("charts:chooser_ranked_export"),
            reverse("charts:denial_appeal_export"),
            reverse("charts:chat_export"),
            reverse("charts:questions_by_procedure_export"),
            reverse("charts:denial_questions_export"),
            reverse("charts:pubmed_article_export"),
        ]
        with patch.dict(os.environ, {"EXPORT_ENABLED": "1"}):
            for url in urls:
                response = self.client.get(url)
                # staff_member_required redirects non-staff to admin login
                self.assertIn(
                    response.status_code,
                    [302, 403],
                    f"Expected redirect/403 for non-staff user at {url}",
                )


class ChooserRankedExportContentTest(TestCase):
    """Test the content of the chooser ranked export."""

    def setUp(self):
        self.staff_user = User.objects.create_user(
            username="staffuser", password="testpass123", is_staff=True
        )
        self.client.login(username="staffuser", password="testpass123")

    def test_chooser_ranked_export_content(self):
        task = ChooserTask.objects.create(
            task_type="appeal",
            status="READY",
            context_json={"denial_summary": "Test denial"},
            source="synthetic",
            num_candidates_expected=2,
            num_candidates_generated=2,
        )
        candidate_a = ChooserCandidate.objects.create(
            task=task,
            candidate_index=0,
            kind="appeal_letter",
            model_name="model-a",
            content="Appeal text A",
        )
        candidate_b = ChooserCandidate.objects.create(
            task=task,
            candidate_index=1,
            kind="appeal_letter",
            model_name="model-b",
            content="Appeal text B",
        )
        # candidate_a gets 2 votes, candidate_b gets 1
        ChooserVote.objects.create(
            task=task,
            chosen_candidate=candidate_a,
            presented_candidate_ids=[candidate_a.id, candidate_b.id],
            session_key="session-1",
        )
        ChooserVote.objects.create(
            task=task,
            chosen_candidate=candidate_a,
            presented_candidate_ids=[candidate_a.id, candidate_b.id],
            session_key="session-2",
        )
        ChooserVote.objects.create(
            task=task,
            chosen_candidate=candidate_b,
            presented_candidate_ids=[candidate_a.id, candidate_b.id],
            session_key="session-3",
        )

        with patch.dict(os.environ, {"EXPORT_ENABLED": "1"}):
            response = self.client.get(reverse("charts:chooser_ranked_export"))

        self.assertEqual(response.status_code, 200)
        content = b"".join(response.streaming_content).decode()
        lines = [json.loads(line) for line in content.strip().split("\n")]
        self.assertEqual(len(lines), 1)

        record = lines[0]
        self.assertEqual(record["task_id"], task.id)
        self.assertEqual(record["task_type"], "appeal")
        self.assertEqual(record["chosen_candidate"]["id"], candidate_a.id)
        self.assertEqual(record["chosen_candidate"]["content"], "Appeal text A")
        self.assertEqual(record["vote_count"], 2)
        self.assertEqual(record["total_votes"], 3)
        self.assertEqual(len(record["all_candidates"]), 2)


class DenialAppealExportContentTest(TestCase):
    """Test the content of the denial + appeal export."""

    def setUp(self):
        self.staff_user = User.objects.create_user(
            username="staffuser", password="testpass123", is_staff=True
        )
        self.client.login(username="staffuser", password="testpass123")

    def _create_denial(self, **kwargs):
        defaults = {
            "hashed_email": "testhash123",
            "denial_text": "Your claim was denied",
            "procedure": "MRI",
            "diagnosis": "Back pain",
            "insurance_company": "TestInsurance",
        }
        defaults.update(kwargs)
        return Denial.objects.create(**defaults)

    def test_denial_appeal_export_with_chosen_proposed(self):
        denial = self._create_denial()
        ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text="This is the chosen appeal text",
            chosen=True,
        )

        with patch.dict(os.environ, {"EXPORT_ENABLED": "1"}):
            response = self.client.get(reverse("charts:denial_appeal_export"))

        self.assertEqual(response.status_code, 200)
        content = b"".join(response.streaming_content).decode()
        lines = [json.loads(line) for line in content.strip().split("\n")]
        self.assertEqual(len(lines), 1)

        record = lines[0]
        self.assertEqual(record["denial_text"], "Your claim was denied")
        self.assertEqual(record["appeal_text"], "This is the chosen appeal text")
        self.assertEqual(record["procedure"], "MRI")

    def test_denial_appeal_export_prefers_manual_deid(self):
        denial = self._create_denial(
            manual_deidentified_denial="De-id denial text",
            manual_deidentified_appeal="De-id appeal text",
            verified_procedure="Verified MRI",
            verified_diagnosis="Verified back pain",
        )
        ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text="Raw appeal text",
            chosen=True,
        )

        with patch.dict(os.environ, {"EXPORT_ENABLED": "1"}):
            response = self.client.get(reverse("charts:denial_appeal_export"))

        content = b"".join(response.streaming_content).decode()
        lines = [json.loads(line) for line in content.strip().split("\n")]
        record = lines[0]
        self.assertEqual(record["denial_text"], "De-id denial text")
        self.assertEqual(record["appeal_text"], "De-id appeal text")
        self.assertEqual(record["procedure"], "Verified MRI")
        self.assertEqual(record["diagnosis"], "Verified back pain")

    def test_denial_appeal_export_excludes_pro_denials(self):
        from fhi_users.models import ProfessionalUser

        pro_user_auth = User.objects.create_user(
            username="prouser", password="testpass123"
        )
        pro = ProfessionalUser.objects.create(user=pro_user_auth, active=True)
        denial = self._create_denial(creating_professional=pro)
        ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text="Pro appeal text",
            chosen=True,
        )

        with patch.dict(os.environ, {"EXPORT_ENABLED": "1"}):
            response = self.client.get(reverse("charts:denial_appeal_export"))

        content = b"".join(response.streaming_content).decode()
        self.assertEqual(content.strip(), "")

    def test_denial_appeal_export_excludes_flagged(self):
        denial = self._create_denial(flag_for_exclude=True)
        ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text="Flagged appeal",
            chosen=True,
        )

        with patch.dict(os.environ, {"EXPORT_ENABLED": "1"}):
            response = self.client.get(reverse("charts:denial_appeal_export"))

        content = b"".join(response.streaming_content).decode()
        self.assertEqual(content.strip(), "")

    def test_denial_appeal_export_with_finalized_appeal(self):
        denial = self._create_denial()
        Appeal.objects.create(
            for_denial=denial,
            appeal_text="Finalized appeal text",
            hashed_email="testhash123",
        )

        with patch.dict(os.environ, {"EXPORT_ENABLED": "1"}):
            response = self.client.get(reverse("charts:denial_appeal_export"))

        content = b"".join(response.streaming_content).decode()
        lines = [json.loads(line) for line in content.strip().split("\n")]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["appeal_text"], "Finalized appeal text")

    def test_denial_appeal_export_includes_qa_and_pubmed_context(self):
        denial = self._create_denial(
            pubmed_context="PubMed says treatment is effective per PMID 12345",
        )
        ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text="Appeal with context",
            chosen=True,
        )
        DenialQA.objects.create(
            denial=denial,
            question="Was prior authorization obtained?",
            text_answer="Yes, on 2024-01-15",
        )
        DenialQA.objects.create(
            denial=denial,
            question="Is the treatment medically necessary?",
            bool_answer=True,
        )

        with patch.dict(os.environ, {"EXPORT_ENABLED": "1"}):
            response = self.client.get(reverse("charts:denial_appeal_export"))

        content = b"".join(response.streaming_content).decode()
        lines = [json.loads(line) for line in content.strip().split("\n")]
        self.assertEqual(len(lines), 1)

        record = lines[0]
        self.assertEqual(
            record["pubmed_context"],
            "PubMed says treatment is effective per PMID 12345",
        )
        self.assertEqual(len(record["qa_pairs"]), 2)
        qa_questions = {qa["question"] for qa in record["qa_pairs"]}
        self.assertIn("Was prior authorization obtained?", qa_questions)
        self.assertIn("Is the treatment medically necessary?", qa_questions)
        # Check text answer
        text_qa = next(
            qa
            for qa in record["qa_pairs"]
            if qa["question"] == "Was prior authorization obtained?"
        )
        self.assertEqual(text_qa["answer"], "Yes, on 2024-01-15")
        # Check bool answer
        bool_qa = next(
            qa
            for qa in record["qa_pairs"]
            if qa["question"] == "Is the treatment medically necessary?"
        )
        self.assertTrue(bool_qa["answer"])


class ChatExportContentTest(TestCase):
    """Test the content of the chat export."""

    def setUp(self):
        self.staff_user = User.objects.create_user(
            username="staffuser", password="testpass123", is_staff=True
        )
        self.client.login(username="staffuser", password="testpass123")

    def test_chat_export_content(self):
        chat = OngoingChat.objects.create(
            chat_history=[
                {"role": "user", "content": "My claim was denied"},
                {"role": "assistant", "content": "I can help you appeal"},
            ],
            denied_item="MRI scan",
            denied_reason="Not medically necessary",
            hashed_email="chathash123",
        )
        Appeal.objects.create(
            chat=chat,
            appeal_text="Chat-based appeal text",
            hashed_email="chathash123",
        )

        with patch.dict(os.environ, {"EXPORT_ENABLED": "1"}):
            response = self.client.get(reverse("charts:chat_export"))

        self.assertEqual(response.status_code, 200)
        content = b"".join(response.streaming_content).decode()
        lines = [json.loads(line) for line in content.strip().split("\n")]
        self.assertEqual(len(lines), 1)

        record = lines[0]
        self.assertEqual(record["denied_item"], "MRI scan")
        self.assertEqual(len(record["chat_history"]), 2)
        self.assertEqual(record["appeal_texts"], ["Chat-based appeal text"])

    def test_chat_export_excludes_pro_chats(self):
        from fhi_users.models import ProfessionalUser

        pro_user_auth = User.objects.create_user(
            username="prouser", password="testpass123"
        )
        pro = ProfessionalUser.objects.create(user=pro_user_auth, active=True)
        OngoingChat.objects.create(
            chat_history=[{"role": "user", "content": "Pro chat"}],
            professional_user=pro,
            hashed_email="prohash123",
        )

        with patch.dict(os.environ, {"EXPORT_ENABLED": "1"}):
            response = self.client.get(reverse("charts:chat_export"))

        content = b"".join(response.streaming_content).decode()
        self.assertEqual(content.strip(), "")

    def test_chat_export_without_appeal(self):
        """Chats without appeals should still be exported."""
        OngoingChat.objects.create(
            chat_history=[
                {"role": "user", "content": "My claim was denied"},
            ],
            denied_item="X-ray",
            hashed_email="noappealhash",
        )

        with patch.dict(os.environ, {"EXPORT_ENABLED": "1"}):
            response = self.client.get(reverse("charts:chat_export"))

        content = b"".join(response.streaming_content).decode()
        lines = [json.loads(line) for line in content.strip().split("\n")]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["appeal_texts"], [])


class QuestionsByProcedureExportContentTest(TestCase):
    """Test the content of the questions by procedure export."""

    def setUp(self):
        self.staff_user = User.objects.create_user(
            username="staffuser", password="testpass123", is_staff=True
        )
        self.client.login(username="staffuser", password="testpass123")

    def test_questions_by_procedure_export_content(self):
        GenericQuestionGeneration.objects.create(
            procedure="MRI",
            diagnosis="Back pain",
            generated_questions=[
                ["Was the MRI pre-authorized?", ""],
                ["Has conservative treatment been tried?", "Yes"],
            ],
        )

        with patch.dict(os.environ, {"EXPORT_ENABLED": "1"}):
            response = self.client.get(reverse("charts:questions_by_procedure_export"))

        self.assertEqual(response.status_code, 200)
        content = b"".join(response.streaming_content).decode()
        lines = [json.loads(line) for line in content.strip().split("\n")]
        self.assertEqual(len(lines), 1)

        record = lines[0]
        self.assertEqual(record["procedure"], "MRI")
        self.assertEqual(record["diagnosis"], "Back pain")
        self.assertEqual(len(record["generated_questions"]), 2)
        self.assertEqual(
            record["generated_questions"][0][0], "Was the MRI pre-authorized?"
        )

    def test_questions_by_procedure_export_empty(self):
        with patch.dict(os.environ, {"EXPORT_ENABLED": "1"}):
            response = self.client.get(reverse("charts:questions_by_procedure_export"))

        self.assertEqual(response.status_code, 200)
        content = b"".join(response.streaming_content).decode()
        self.assertEqual(content.strip(), "")


class DenialQuestionsExportContentTest(TestCase):
    """Test the content of the denial questions export."""

    def setUp(self):
        self.staff_user = User.objects.create_user(
            username="staffuser", password="testpass123", is_staff=True
        )
        self.client.login(username="staffuser", password="testpass123")

    def _create_denial(self, **kwargs):
        defaults = {
            "hashed_email": "testhash456",
            "denial_text": "Your claim was denied",
            "procedure": "MRI",
            "diagnosis": "Back pain",
            "insurance_company": "TestInsurance",
            "generated_questions": [
                ["Was the MRI pre-authorized?", ""],
                ["Has conservative treatment been tried?", "Yes"],
                ["Is the diagnosis confirmed by imaging?", ""],
            ],
        }
        defaults.update(kwargs)
        return Denial.objects.create(**defaults)

    def test_denial_questions_export_content(self):
        denial = self._create_denial()
        # Mark one question as answered
        DenialQA.objects.create(
            denial=denial,
            question="Was the MRI pre-authorized?",
            text_answer="Yes, it was pre-authorized",
        )

        with patch.dict(os.environ, {"EXPORT_ENABLED": "1"}):
            response = self.client.get(reverse("charts:denial_questions_export"))

        self.assertEqual(response.status_code, 200)
        content = b"".join(response.streaming_content).decode()
        lines = [json.loads(line) for line in content.strip().split("\n")]
        self.assertEqual(len(lines), 1)

        record = lines[0]
        self.assertEqual(record["procedure"], "MRI")
        self.assertEqual(len(record["questions"]), 3)

        # Answered questions should come first
        self.assertTrue(record["questions"][0]["was_answered"])
        self.assertEqual(
            record["questions"][0]["question"], "Was the MRI pre-authorized?"
        )
        # Remaining should be unanswered
        self.assertFalse(record["questions"][1]["was_answered"])
        self.assertFalse(record["questions"][2]["was_answered"])

    def test_denial_questions_export_excludes_pro_denials(self):
        from fhi_users.models import ProfessionalUser

        pro_user_auth = User.objects.create_user(
            username="prouser", password="testpass123"
        )
        pro = ProfessionalUser.objects.create(user=pro_user_auth, active=True)
        self._create_denial(creating_professional=pro)

        with patch.dict(os.environ, {"EXPORT_ENABLED": "1"}):
            response = self.client.get(reverse("charts:denial_questions_export"))

        content = b"".join(response.streaming_content).decode()
        self.assertEqual(content.strip(), "")

    def test_denial_questions_export_excludes_no_questions(self):
        self._create_denial(generated_questions=None)

        with patch.dict(os.environ, {"EXPORT_ENABLED": "1"}):
            response = self.client.get(reverse("charts:denial_questions_export"))

        content = b"".join(response.streaming_content).decode()
        self.assertEqual(content.strip(), "")

    def test_denial_questions_export_uses_verified_fields(self):
        self._create_denial(
            verified_procedure="Verified MRI",
            verified_diagnosis="Verified back pain",
        )

        with patch.dict(os.environ, {"EXPORT_ENABLED": "1"}):
            response = self.client.get(reverse("charts:denial_questions_export"))

        content = b"".join(response.streaming_content).decode()
        lines = [json.loads(line) for line in content.strip().split("\n")]
        record = lines[0]
        self.assertEqual(record["procedure"], "Verified MRI")
        self.assertEqual(record["diagnosis"], "Verified back pain")


class PubMedArticleExportContentTest(TestCase):
    """Test the content of the PubMed article summaries export."""

    def setUp(self):
        self.staff_user = User.objects.create_user(
            username="staffuser", password="testpass123", is_staff=True
        )
        self.client.login(username="staffuser", password="testpass123")

    def test_pubmed_article_export_content(self):
        PubMedArticleSummarized.objects.create(
            pmid="12345678",
            doi="10.1234/test",
            query="MRI back pain",
            title="Efficacy of MRI for Back Pain Diagnosis",
            abstract="This study evaluates...",
            basic_summary="MRI is effective for diagnosing back pain.",
            says_effective=True,
            article_url="https://pubmed.ncbi.nlm.nih.gov/12345678/",
        )

        with patch.dict(os.environ, {"EXPORT_ENABLED": "1"}):
            response = self.client.get(reverse("charts:pubmed_article_export"))

        self.assertEqual(response.status_code, 200)
        content = b"".join(response.streaming_content).decode()
        lines = [json.loads(line) for line in content.strip().split("\n")]
        self.assertEqual(len(lines), 1)

        record = lines[0]
        self.assertEqual(record["pmid"], "12345678")
        self.assertEqual(record["doi"], "10.1234/test")
        self.assertEqual(record["title"], "Efficacy of MRI for Back Pain Diagnosis")
        self.assertEqual(
            record["basic_summary"], "MRI is effective for diagnosing back pain."
        )
        self.assertTrue(record["says_effective"])

    def test_pubmed_article_export_excludes_unsummarized(self):
        """Articles without summaries should not be exported."""
        PubMedArticleSummarized.objects.create(
            pmid="99999999",
            query="test query",
            title="Unsummarized Article",
            basic_summary=None,
        )
        PubMedArticleSummarized.objects.create(
            pmid="88888888",
            query="test query 2",
            title="Empty Summary Article",
            basic_summary="",
        )

        with patch.dict(os.environ, {"EXPORT_ENABLED": "1"}):
            response = self.client.get(reverse("charts:pubmed_article_export"))

        content = b"".join(response.streaming_content).decode()
        self.assertEqual(content.strip(), "")

    def test_pubmed_article_export_empty(self):
        with patch.dict(os.environ, {"EXPORT_ENABLED": "1"}):
            response = self.client.get(reverse("charts:pubmed_article_export"))

        self.assertEqual(response.status_code, 200)
        content = b"".join(response.streaming_content).decode()
        self.assertEqual(content.strip(), "")


class RunExportsCommandTest(TestCase):
    """Test the run_exports management command."""

    def test_run_exports_creates_files(self):
        import tempfile
        from django.core.management import call_command

        with tempfile.TemporaryDirectory() as tmpdir:
            call_command("run_exports", output_dir=tmpdir)

            # Check that all expected files were created
            expected_files = [
                "de_identified.jsonl",
                "chooser_ranked.jsonl",
                "denial_appeal.jsonl",
                "chat.jsonl",
                "questions_by_procedure.jsonl",
                "denial_questions.jsonl",
                "pubmed_articles.jsonl",
            ]
            for filename in expected_files:
                filepath = os.path.join(tmpdir, filename)
                self.assertTrue(
                    os.path.exists(filepath),
                    f"Expected export file {filename} to exist",
                )

    def test_run_exports_selective(self):
        import tempfile
        from django.core.management import call_command

        with tempfile.TemporaryDirectory() as tmpdir:
            call_command("run_exports", output_dir=tmpdir, exports=["chat"])

            # Only chat file should exist
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "chat.jsonl")))
            self.assertFalse(
                os.path.exists(os.path.join(tmpdir, "de_identified.jsonl"))
            )
