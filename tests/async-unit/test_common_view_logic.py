import asyncio
import json
import io
from asgiref.sync import async_to_sync
from unittest.mock import Mock, patch, AsyncMock
from typing import AsyncIterator, List
from fighthealthinsurance.common_view_logic import (
    FindNextStepsHelper,
    AppealsBackendHelper,
    NextStepInfo,
    DenialCreatorHelper,
)
from fighthealthinsurance.utils import (
    MIN_APPEAL_CHARS,
    is_real_appeal,
    meaningful_appeal_length,
    warn_too_short_appeal,
)
from fighthealthinsurance.helpers import SendFaxHelper, RemoveDataHelper
from fighthealthinsurance.models import (
    Denial,
    DenialTypes,
    Appeal,
    FaxesToSend,
    Regulator,
)
import pytest
from django.test import TestCase


class TestCommonViewLogic(TestCase):
    fixtures = ["fighthealthinsurance/fixtures/initial.yaml"]

    def _create_test_denial(self, denial_id, gen_attempts=0):
        """Helper to create a test denial with specified gen_attempts."""
        email = "test@example.com"
        denial = Denial.objects.create(
            denial_id=denial_id,
            semi_sekret="sekret",
            hashed_email=Denial.get_hashed_email(email),
            gen_attempts=gen_attempts,
        )
        return email, denial

    @staticmethod
    async def collect_appeal_responses(data):
        """Collect all responses from generate_appeals into categorized lists."""
        status_messages = []
        appeal_contents = []
        raw_chunks = []
        async for chunk in AppealsBackendHelper.generate_appeals(data):
            raw_chunks.append(chunk)
            if not chunk or not chunk.strip():
                continue
            try:
                parsed = json.loads(chunk)
                if parsed.get("type") == "status":
                    status_messages.append(parsed)
                elif "content" in parsed:
                    appeal_contents.append(parsed["content"])
            except json.JSONDecodeError:
                pass
        return status_messages, appeal_contents, raw_chunks

    @pytest.mark.django_db
    @patch("fighthealthinsurance.common_view_logic.Denial.objects")
    def test_remove_data_for_email(self, mock_denial_objects):
        mock_denial = Mock()
        mock_denial_objects.filter.return_value.delete.return_value = 1
        RemoveDataHelper.remove_data_for_email("test@example.com")
        mock_denial_objects.filter.assert_called()
        mock_denial_objects.filter.return_value.delete.assert_called()

    @pytest.mark.django_db
    def test_find_next_steps(self):
        # Create real DenialTypes objects
        insurance_company_type = DenialTypes.objects.get(name="Insurance Company")
        medically_necessary_type = DenialTypes.objects.get(name="Medically Necessary")

        # Create a real Denial object
        email = "test@example.com"
        denial = Denial.objects.create(
            denial_id=1,
            semi_sekret="sekret",
            hashed_email=Denial.get_hashed_email(email),
        )

        # Add denial types to the denial
        denial.denial_type.add(insurance_company_type)
        denial.denial_type.add(medically_necessary_type)

        # Call the function being tested with real objects
        next_steps = FindNextStepsHelper.find_next_steps(
            denial_id=denial.denial_id,
            email=email,
            semi_sekret=denial.semi_sekret,
            procedure="prep",
            plan_id="1",
            denial_type=None,
            denial_date=None,
            diagnosis="high risk homosexual behaviour",
            insurance_company="evilco",
            claim_id=7,
        )

        # Verify the result
        self.assertIsInstance(next_steps, NextStepInfo)

        # Clean up the test data
        denial.delete()

    @pytest.mark.django_db
    def test_find_next_steps_populates_pharmacy_suggestion_for_drug_denial(self):
        """For a denial whose procedure is a known drug (Wegovy), the
        next-steps payload should carry a PharmacyCouponSuggestion so the
        consumer flow can surface GoodRx / Cost Plus / Crush Cost / Amazon
        Pharmacy."""
        from fighthealthinsurance.pharmacy_coupon_detector import (
            PharmacyCouponSuggestion,
        )

        email = "drug-denial@example.com"
        denial = Denial.objects.create(
            denial_id=2001,
            semi_sekret="sekret-drug",
            hashed_email=Denial.get_hashed_email(email),
            procedure="Wegovy",
            diagnosis="Obesity",
            denial_text="Wegovy denied as non-formulary.",
        )
        try:
            next_steps = FindNextStepsHelper.find_next_steps_for_denial(denial, email)
            assert isinstance(
                next_steps.pharmacy_coupon_suggestion, PharmacyCouponSuggestion
            )
            assert next_steps.pharmacy_coupon_suggestion.drug_name == "wegovy"
            # Pin by name set rather than count: option count is fragile as
            # new pharmacy discount programs (e.g. Crush Cost) are added.
            opt_names = {
                opt.name
                for opt in next_steps.pharmacy_coupon_suggestion.pharmacy_options
            }
            assert {
                "GoodRx",
                "Mark Cuban Cost Plus Drugs",
                "Crush Cost",
                "Amazon Search",
            } <= opt_names
        finally:
            denial.delete()

    @pytest.mark.django_db
    def test_find_next_steps_no_pharmacy_suggestion_for_non_drug_denial(self):
        """For a non-drug denial (MRI of knee), no pharmacy suggestion is
        produced and the field stays None - the partial template renders
        nothing in that case."""
        email = "mri-denial@example.com"
        denial = Denial.objects.create(
            denial_id=2002,
            semi_sekret="sekret-mri",
            hashed_email=Denial.get_hashed_email(email),
            procedure="MRI of knee",
            diagnosis="Knee pain",
            denial_text="MRI denied as not medically necessary.",
        )
        try:
            next_steps = FindNextStepsHelper.find_next_steps_for_denial(denial, email)
            assert next_steps.pharmacy_coupon_suggestion is None
            # MRI denials shouldn't surface a financial-assistance section
            # either - has_specific_matches() gates on diagnosis/drug/state
            # matches, and "Knee pain" doesn't match any catalog entry.
            assert next_steps.financial_assistance is None
        finally:
            denial.delete()

    @pytest.mark.django_db
    def test_find_next_steps_populates_financial_assistance_for_hiv_truvada(self):
        """A Truvada (PrEP) denial with an HIV-related diagnosis must
        surface the financial-assistance directory on the consumer flow:
        ADAP / Ryan White matches via the diagnosis keyword, the state
        Medicaid pathway attaches when ``your_state`` is set, and the
        general copay-foundation catalog rides along."""
        from fighthealthinsurance.financial_assistance_directory import (
            FinancialAssistanceResults,
        )

        email = "truvada-prep@example.com"
        denial = Denial.objects.create(
            denial_id=2003,
            semi_sekret="sekret-truvada",
            hashed_email=Denial.get_hashed_email(email),
            procedure="Truvada",
            diagnosis="HIV pre-exposure prophylaxis",
            denial_text="Truvada denied as non-formulary.",
            your_state="CA",
        )
        try:
            next_steps = FindNextStepsHelper.find_next_steps_for_denial(denial, email)
            assert isinstance(
                next_steps.financial_assistance, FinancialAssistanceResults
            )
            diagnosis_names = [
                p.name for p in next_steps.financial_assistance.diagnosis_specific
            ]
            assert any("ADAP" in n for n in diagnosis_names), diagnosis_names
            safety_net_names = [
                p.name for p in next_steps.financial_assistance.safety_net
            ]
            assert any("Ryan White" in n for n in safety_net_names), safety_net_names
            # State pathway attached for CA via state_help.
            assert next_steps.financial_assistance.state_medicaid_name is not None
        finally:
            denial.delete()

    @pytest.mark.django_db
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_generate_appeals(self, mock_appeal_generator):
        email = "test@example.com"
        denial = Denial.objects.create(
            denial_id=1,
            semi_sekret="sekret",
            hashed_email=Denial.get_hashed_email(email),
        )

        async def async_generator(items) -> AsyncIterator[str]:
            """Test helper: Async generator yielding items with delay."""
            for item in items:
                await asyncio.sleep(0.1)
                yield item

        async def test():
            mock_appeal_generator.generate_appeals.return_value = async_generator(
                ["test"]
            )
            responses = AppealsBackendHelper.generate_appeals(
                {
                    "denial_id": 1,
                    "email": email,
                    "semi_sekret": denial.semi_sekret,
                }
            )
            buf = io.StringIO()

            async for chunk in responses:
                buf.write(chunk)

            buf.seek(0)
            string_data = buf.getvalue()

        async_to_sync(test)()

    @pytest.mark.django_db
    @patch("fighthealthinsurance.helpers.fax_helpers.fax_actor_ref")
    def test_store_fax_number_as_destination(self, mock_fax_actor_ref):
        """Test that the fax number from a denial is stored as the destination in FaxesToSend."""
        # Create test data
        email = "test@example.com"
        fax_number = "1234567890"

        # Create a denial with a fax number
        denial = Denial.objects.create(
            denial_id=1,
            semi_sekret="sekret",
            hashed_email=Denial.get_hashed_email(email),
            appeal_fax_number=fax_number,
        )

        # Create an appeal
        appeal = Appeal.objects.create(
            for_denial=denial,
            appeal_text="Test appeal text",
            hashed_email=Denial.get_hashed_email(email),
        )

        # Set up mock
        mock_fax_actor_ref.get.do_send_fax.remote.return_value = None

        # Call the method under test
        result = SendFaxHelper.stage_appeal_as_fax(appeal=appeal, email=email)

        # Verify the fax was created with the correct destination
        fax = FaxesToSend.objects.get(uuid=result.uuid)
        self.assertEqual(fax.destination, fax_number)
        self.assertEqual(fax.denial_id, denial)
        self.assertEqual(fax.appeal_text, appeal.appeal_text)

        # Verify the fax actor was called to send the fax
        mock_fax_actor_ref.get.do_send_fax.remote.assert_called_once_with(
            fax.hashed_email, str(fax.uuid)
        )

    @pytest.mark.django_db
    @patch("fighthealthinsurance.helpers.fax_helpers.fax_actor_ref")
    def test_resend_sets_should_send_and_sent_flags(self, mock_fax_actor_ref):
        """Test that resend properly sets should_send=True and sent=False."""
        # Create test data
        email = "test@example.com"
        hashed_email = Denial.get_hashed_email(email)
        fax_number = "9876543210"
        new_fax_number = "1234567890"

        # Create a denial
        denial = Denial.objects.create(
            denial_id=1,
            semi_sekret="sekret",
            hashed_email=hashed_email,
            appeal_fax_number=fax_number,
        )

        # Create an appeal
        appeal = Appeal.objects.create(
            for_denial=denial,
            appeal_text="Test appeal text for resend",
            hashed_email=hashed_email,
        )

        # Create a fax that was already sent (simulating a previously sent fax)
        fax = FaxesToSend.objects.create(
            hashed_email=hashed_email,
            email=email,
            appeal_text=appeal.appeal_text,
            destination=fax_number,
            denial_id=denial,
            for_appeal=appeal,
            paid=True,
            sent=True,  # Already sent
            should_send=False,  # Should not send again
        )

        # Set up mock
        mock_fax_actor_ref.get.do_send_fax.remote.return_value = None

        # Call the resend method (uuid must be string per method signature)
        result = SendFaxHelper.resend(new_fax_number, str(fax.uuid), hashed_email)

        # Verify the method returned True
        self.assertTrue(result)

        # Verify the fax was updated correctly
        updated_fax = FaxesToSend.objects.get(uuid=fax.uuid)
        self.assertEqual(updated_fax.destination, new_fax_number)
        self.assertTrue(updated_fax.should_send)
        self.assertFalse(updated_fax.sent)

        # Verify the fax actor was called to send the fax
        mock_fax_actor_ref.get.do_send_fax.remote.assert_called_once_with(
            hashed_email, str(fax.uuid)
        )

    @pytest.mark.django_db
    @patch(
        "fighthealthinsurance.common_view_logic.fire_and_forget_in_new_threadpool",
        new_callable=AsyncMock,
    )
    @patch(
        "fighthealthinsurance.common_view_logic.MLAppealQuestionsHelper.generate_questions_for_denial",
        new_callable=AsyncMock,
    )
    def test_generate_appeal_questions(self, mock_generate_questions, mock_fire_forget):
        """Test that generate_appeal_questions generates and stores questions."""
        test_questions = [
            (
                "What medical evidence supports the necessity of this treatment?",
                "Clinical studies show efficacy",
            ),
            ("Has the patient tried alternative treatments?", ""),
            (
                "How does this treatment align with current medical guidelines?",
                "It follows AMA recommendations",
            ),
        ]
        mock_generate_questions.return_value = test_questions

        async def test():
            email = "test@example.com"
            denial = await Denial.objects.acreate(
                denial_id=99,
                semi_sekret="sekret",
                hashed_email=Denial.get_hashed_email(email),
                denial_text="This is a test denial for medical service",
                health_history="Patient has a history of condition X",
            )

            try:
                # Call the method being tested
                questions = await DenialCreatorHelper.generate_appeal_questions(
                    denial.denial_id
                )

                # Verify the questions were returned correctly
                self.assertEqual(questions, test_questions)

                # Verify ML helper was called
                mock_generate_questions.assert_called_once()

                # Verify the questions were stored in the denial object
                updated_denial = await Denial.objects.aget(denial_id=denial.denial_id)

                # Django's JSON serialization converts tuples to lists
                stored_questions_as_tuples = [
                    (q[0], q[1]) if isinstance(q, list) else q
                    for q in updated_denial.generated_questions
                ]
                self.assertEqual(stored_questions_as_tuples, test_questions)
            finally:
                await Denial.objects.filter(denial_id=99).adelete()

        async_to_sync(test)()

    @pytest.mark.django_db
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_generate_appeals_skips_research_on_high_gen_attempts(
        self, mock_appeal_generator
    ):
        """Test that research phase is skipped when gen_attempts >= 3."""
        email, denial = self._create_test_denial(12, gen_attempts=3)
        mock_appeal_generator.make_appeals.return_value = iter(
            ["Dear Insurance Company, this is an appeal."]
        )

        async def test():
            try:
                status_messages, appeal_contents, _ = (
                    await self.collect_appeal_responses(
                        {
                            "denial_id": 12,
                            "email": email,
                            "semi_sekret": denial.semi_sekret,
                        }
                    )
                )

                # Check for the skip message
                research_messages = [
                    m for m in status_messages if m.get("phase") == "research"
                ]
                assert (
                    len(research_messages) > 0
                ), f"Should have a research skip status, got: {status_messages}"
                skip_msg = research_messages[0]
                assert (
                    skip_msg.get("substep") == "all"
                ), f"Skip message should have substep 'all': {skip_msg}"
                assert (
                    "skip" in skip_msg.get("message", "").lower()
                ), f"Skip message should mention 'skip': {skip_msg}"
            finally:
                await Denial.objects.filter(denial_id=12).adelete()

        async_to_sync(test)()

    @pytest.mark.django_db
    @patch(
        "fighthealthinsurance.common_view_logic.get_rag_context_for_denial",
        new_callable=AsyncMock,
        return_value=None,
    )
    @patch(
        "fighthealthinsurance.common_view_logic.MLCitationsHelper.generate_citations_for_denial",
        new_callable=AsyncMock,
        return_value=None,
    )
    @patch("fighthealthinsurance.common_view_logic.AppealsBackendHelper.pmt")
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_generate_appeals_has_phase_field(
        self, mock_appeal_generator, mock_pmt, mock_ml_citations, mock_rag
    ):
        """Test that status messages include the phase field."""
        mock_pmt.find_context_for_denial = AsyncMock(return_value=None)
        email, denial = self._create_test_denial(13, gen_attempts=0)
        mock_appeal_generator.make_appeals.return_value = iter(
            ["Dear Insurance Company, this is an appeal."]
        )

        async def test():
            try:
                status_messages, _, _ = await self.collect_appeal_responses(
                    {
                        "denial_id": 13,
                        "email": email,
                        "semi_sekret": denial.semi_sekret,
                    }
                )

                # Verify phase field is present on status messages
                statuses_with_phase = [s for s in status_messages if "phase" in s]
                assert (
                    len(statuses_with_phase) > 0
                ), f"Status messages should include 'phase' field, got: {status_messages}"

                # Verify phase progression order
                phases_seen = []
                for msg in status_messages:
                    phase = msg.get("phase")
                    if phase and (not phases_seen or phases_seen[-1] != phase):
                        phases_seen.append(phase)
                assert (
                    phases_seen[0] == "init"
                ), f"First phase should be 'init', got: {phases_seen}"
                assert (
                    "generating" in phases_seen
                ), f"Should see 'generating' phase, got: {phases_seen}"
            finally:
                await Denial.objects.filter(denial_id=13).adelete()

        async_to_sync(test)()

    @pytest.mark.django_db
    @patch(
        "fighthealthinsurance.common_view_logic.get_rag_context_for_denial",
        new_callable=AsyncMock,
        return_value=None,
    )
    @patch(
        "fighthealthinsurance.common_view_logic.MLCitationsHelper.generate_citations_for_denial",
        new_callable=AsyncMock,
        return_value=None,
    )
    @patch("fighthealthinsurance.common_view_logic.AppealsBackendHelper.pmt")
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_rag_lookup_forwards_cpt_and_hcpcs_codes(
        self, mock_appeal_generator, mock_pmt, mock_ml_citations, mock_rag
    ):
        """The RAG context builder must extract CPT *and* HCPCS Level II
        codes from denial text and forward them to the RAG service via
        ``procedure_codes``. A regression that drops HCPCS support (e.g.
        reverting to the CPT-only regex) would leave DME-coded denials
        without enriched RAG context."""
        mock_pmt.find_context_for_denial = AsyncMock(return_value=None)
        email = "test@example.com"
        denial = Denial.objects.create(
            denial_id=14,
            semi_sekret="sekret",
            hashed_email=Denial.get_hashed_email(email),
            gen_attempts=0,
            denial_text=(
                "Service with CPT 99213 and DME E0260 hospital bed denied. "
                "Diagnosis Z00.00 documented."
            ),
        )
        mock_appeal_generator.make_appeals.return_value = iter(
            ["Dear Insurance Company, this is an appeal."]
        )

        async def test():
            try:
                await self.collect_appeal_responses(
                    {
                        "denial_id": 14,
                        "email": email,
                        "semi_sekret": denial.semi_sekret,
                    }
                )

                mock_rag.assert_called()
                _args, kwargs = mock_rag.call_args
                forwarded_codes = kwargs.get("procedure_codes") or []
                # Both the CPT code and the HCPCS DME code should be
                # forwarded for RAG enrichment.
                assert (
                    "99213" in forwarded_codes
                ), f"CPT 99213 missing from procedure_codes: {forwarded_codes}"
                assert (
                    "E0260" in forwarded_codes
                ), f"HCPCS E0260 missing from procedure_codes: {forwarded_codes}"
                forwarded_diagnosis = kwargs.get("diagnosis_codes") or []
                assert "Z00.00" in forwarded_diagnosis, (
                    f"ICD-10 Z00.00 missing from diagnosis_codes: "
                    f"{forwarded_diagnosis}"
                )
            finally:
                await Denial.objects.filter(denial_id=14).adelete()

        async_to_sync(test)()

    @pytest.mark.django_db
    @patch(
        "fighthealthinsurance.common_view_logic.fire_and_forget_in_new_threadpool",
        new_callable=AsyncMock,
    )
    @patch(
        "fighthealthinsurance.common_view_logic.appealGenerator.get_procedure_and_diagnosis",
        new_callable=AsyncMock,
        return_value=("pembrolizumab", "melanoma"),
    )
    def test_extract_schedules_clinical_trials_prefetch(
        self, _mock_get_proc_diag, mock_fire_forget
    ):
        """``extract_set_denial_and_diagnosis`` must fire the ClinicalTrials
        prefetch alongside the existing PubMed and speculative-context tasks
        once procedure/diagnosis are populated."""
        email = "test@example.com"
        denial = Denial.objects.create(
            denial_id=15,
            semi_sekret="sekret",
            hashed_email=Denial.get_hashed_email(email),
            denial_text="Insurer denied pembrolizumab for melanoma as experimental.",
        )

        async def test():
            try:
                await DenialCreatorHelper.extract_set_denial_and_diagnosis(
                    denial.denial_id
                )

                # All three background tasks must have been scheduled.
                assert mock_fire_forget.await_count == 3, (
                    f"Expected 3 fire-and-forget tasks (pubmed, clinical "
                    f"trials, speculative context); saw "
                    f"{mock_fire_forget.await_count}"
                )
                scheduled_names = [
                    call.args[0].cr_code.co_name
                    for call in mock_fire_forget.await_args_list
                ]
                assert "find_clinical_trials" in scheduled_names, (
                    f"ClinicalTrials prefetch coroutine not scheduled; "
                    f"saw {scheduled_names}"
                )
                # Close the captured coroutines so pytest doesn't warn about
                # un-awaited coros from the mocked-out fire_and_forget.
                for call in mock_fire_forget.await_args_list:
                    call.args[0].close()
            finally:
                await Denial.objects.filter(denial_id=15).adelete()

        async_to_sync(test)()

    @pytest.mark.django_db
    @patch(
        "fighthealthinsurance.common_view_logic.fire_and_forget_in_new_threadpool",
        new_callable=AsyncMock,
    )
    @patch(
        "fighthealthinsurance.common_view_logic.ClinicalTrialsTools.find_trials_for_denial",
        new_callable=AsyncMock,
        side_effect=RuntimeError("simulated upstream failure"),
    )
    @patch(
        "fighthealthinsurance.common_view_logic.appealGenerator.get_procedure_and_diagnosis",
        new_callable=AsyncMock,
        return_value=("pembrolizumab", "melanoma"),
    )
    def test_clinical_trials_prefetch_swallows_failures(
        self, _mock_get_proc_diag, _mock_find_trials, mock_fire_forget
    ):
        """The ``find_clinical_trials`` inner coroutine must swallow every
        exception so the daemon thread that ``fire_and_forget_in_new_threadpool``
        spawns can never re-raise into the appeal flow."""
        email = "test@example.com"
        denial = Denial.objects.create(
            denial_id=16,
            semi_sekret="sekret",
            hashed_email=Denial.get_hashed_email(email),
            denial_text="Insurer denied pembrolizumab for melanoma as experimental.",
        )

        async def test():
            try:
                await DenialCreatorHelper.extract_set_denial_and_diagnosis(
                    denial.denial_id
                )

                # Locate the captured clinical-trials coroutine and run it
                # directly. If the inner try/except is correct, awaiting it
                # must not raise even though find_trials_for_denial throws.
                ct_coros = [
                    call.args[0]
                    for call in mock_fire_forget.await_args_list
                    if call.args[0].cr_code.co_name == "find_clinical_trials"
                ]
                assert len(ct_coros) == 1, (
                    f"Expected exactly one find_clinical_trials coro; "
                    f"got {len(ct_coros)}"
                )
                # Should complete cleanly despite the upstream RuntimeError.
                await ct_coros[0]

                # Close any remaining captured coros to avoid pytest warnings.
                for call in mock_fire_forget.await_args_list:
                    coro = call.args[0]
                    if coro.cr_code.co_name != "find_clinical_trials":
                        coro.close()
            finally:
                await Denial.objects.filter(denial_id=16).adelete()

        async_to_sync(test)()


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, False),
        ("", False),
        ("   \t\n  ", False),  # whitespace-only
        ("ok", False),  # below threshold
        ("a" * (MIN_APPEAL_CHARS - 1), False),  # just below threshold
        ("a" * MIN_APPEAL_CHARS, True),  # at threshold (inclusive >=)
        (123, False),  # non-string
        (["a"] * 100, False),  # non-string
        ("          short          ", False),  # strip-then-measure
        ("a" * (MIN_APPEAL_CHARS + 1), True),  # just over threshold
        ("this is a long enough appeal text for delivery", True),
        # Internal whitespace doesn't count: 10 letters padded with spaces.
        ("a " * 10, False),
        # Control characters don't count: 10 letters + 50 NUL bytes.
        ("a" * 10 + "\x00" * 50, False),
        # Exactly MIN_APPEAL_CHARS letters with control padding stays valid.
        ("a" * MIN_APPEAL_CHARS + "\x07" * 20, True),
        # Tabs/newlines between letters are ignored (only 12 real letters).
        ("a\tb\nc d e f g h i j k l", False),
    ],
)
def test_is_real_appeal(value, expected):
    assert is_real_appeal(value) is expected


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, 0),
        (123, 0),
        ("", 0),
        ("   \t\n  ", 0),  # whitespace-only
        ("abc", 3),
        ("a b c", 3),  # internal spaces excluded
        ("  hi  ", 2),  # surrounding spaces excluded
        ("a\x00b\x07c", 3),  # control characters excluded
        ("a" * 15, 15),
        ("a" * 15 + "\n\n\n", 15),  # trailing whitespace excluded
    ],
)
def test_meaningful_appeal_length(value, expected):
    assert meaningful_appeal_length(value) == expected


def test_warn_too_short_appeal_logs_length_and_context():
    """The shared drop-site warning reports the measured length, the
    threshold, and the caller-supplied context. The reported length is the
    meaningful (non-whitespace) count, so internal spaces are not counted."""
    from loguru import logger as loguru_logger

    sink = io.StringIO()
    handler_id = loguru_logger.add(sink, level="WARNING")
    try:
        warn_too_short_appeal("a b c", "model='m' for denial 7")
    finally:
        loguru_logger.remove(handler_id)
    output = sink.getvalue()
    assert "too-short appeal" in output
    assert "len=3" in output  # 3 letters, the 2 spaces are excluded
    assert f"< {MIN_APPEAL_CHARS} chars" in output
    assert "model='m' for denial 7" in output


def test_warn_too_short_appeal_handles_non_string():
    """A non-string (e.g. None) is reported as length 0 without raising."""
    from loguru import logger as loguru_logger

    sink = io.StringIO()
    handler_id = loguru_logger.add(sink, level="WARNING")
    try:
        warn_too_short_appeal(None, "some context")
    finally:
        loguru_logger.remove(handler_id)
    output = sink.getvalue()
    assert "len=0" in output


class RegulatorContactInfoTest(TestCase):
    """Denials matched to a regulator surface that regulator's contact info."""

    fixtures = ["fighthealthinsurance/fixtures/initial.yaml"]

    def _make_denial(self, denial_text="denied"):
        return Denial.objects.create(
            denial_text=denial_text,
            hashed_email=Denial.get_hashed_email("regulator@example.com"),
        )

    def test_extract_set_regulator_links_erisa_denial(self):
        denial = self._make_denial(
            "You may have the right to file a civil action under ERISA."
        )
        async_to_sync(DenialCreatorHelper.extract_set_regulator)(denial.denial_id)
        denial.refresh_from_db()
        self.assertIsNotNone(denial.regulator)
        self.assertEqual(denial.regulator.alt_name, "ERISA")

    def test_extract_set_regulator_leaves_unmatched_denial_null(self):
        denial = self._make_denial("Nothing relevant in this text.")
        async_to_sync(DenialCreatorHelper.extract_set_regulator)(denial.denial_id)
        denial.refresh_from_db()
        self.assertIsNone(denial.regulator)

    def test_extract_set_regulator_does_not_overwrite_existing_match(self):
        cms = Regulator.objects.get(alt_name="CMS")
        denial = self._make_denial(
            "You may have the right to file a civil action under ERISA."
        )
        denial.regulator = cms
        denial.save()
        async_to_sync(DenialCreatorHelper.extract_set_regulator)(denial.denial_id)
        denial.refresh_from_db()
        self.assertEqual(denial.regulator.alt_name, "CMS")

    def test_outside_help_includes_regulator_phone_number(self):
        erisa = Regulator.objects.get(alt_name="ERISA")
        denial = self._make_denial()
        denial.regulator = erisa
        denial.save()
        details = FindNextStepsHelper._get_outside_help_details(denial)
        flattened = " ".join(f"{option} {how}" for option, how in details)
        self.assertIn("1-866-444-3272", flattened)
        self.assertIn("Federal Department of Labor", flattened)

    def test_outside_help_has_no_contact_row_without_matched_regulator(self):
        denial = self._make_denial()
        details = FindNextStepsHelper._get_outside_help_details(denial)
        flattened = " ".join(f"{option} {how}" for option, how in details)
        self.assertNotIn("file a complaint online", flattened)
