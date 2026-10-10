from unittest.mock import patch, AsyncMock, MagicMock
import pytest

from fighthealthinsurance.ml.ml_citations_helper import MLCitationsHelper
from fighthealthinsurance.models import Denial, ECRIGuideline


class TestMLCitationsHelper:
    """Tests for the MLCitationsHelper class.

    Note: These tests mock the module-level ml_router and database access.
    The API has changed from earlier versions - generate_citations() no longer
    exists, replaced by generate_generic_citations() and generate_specific_citations().
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        """Set up test fixtures."""
        self.mock_backend = AsyncMock()
        self.mock_backend.get_citations.return_value = ["Citation 1", "Citation 2"]

        # Create a mock denial
        self.mock_denial = MagicMock(spec=Denial)
        self.mock_denial.denial_id = 12345
        self.mock_denial.denial_text = "Test denial text"
        self.mock_denial.procedure = "Test procedure"
        self.mock_denial.diagnosis = "Test diagnosis"
        self.mock_denial.health_history = "Test health history"
        self.mock_denial.plan_context = "Test plan context"
        self.mock_denial.use_external = False
        self.mock_denial.ml_citation_context = None
        self.mock_denial.candidate_ml_citation_context = None
        self.mock_denial.candidate_procedure = None
        self.mock_denial.candidate_diagnosis = None
        self.mock_denial.microsite_slug = None

    @pytest.mark.asyncio
    @patch("fighthealthinsurance.ml.ml_citations_helper.ml_router")
    async def test_generate_specific_citations(self, mock_ml_router):
        """Test generate_specific_citations method."""
        mock_ml_router.full_find_citation_backends.return_value = [self.mock_backend]

        with patch(
            "fighthealthinsurance.ml.ml_citations_helper.best_within_timelimit",
            new_callable=AsyncMock,
            return_value=["Citation 1", "Citation 2"],
        ):
            citations = await MLCitationsHelper.generate_specific_citations(
                denial=self.mock_denial
            )

            assert len(citations) == 2
            assert citations[0] == "Citation 1"

    @pytest.mark.asyncio
    @patch("fighthealthinsurance.ml.ml_citations_helper.ml_router")
    async def test_generate_specific_citations_no_backends(self, mock_ml_router):
        """Test behavior when no backends are available."""
        mock_ml_router.full_find_citation_backends.return_value = []

        citations = await MLCitationsHelper.generate_specific_citations(
            denial=self.mock_denial
        )

        assert citations == []

    @pytest.mark.asyncio
    async def test_generate_specific_citations_no_context(self):
        """Test that generate_specific_citations returns empty when no context."""
        # Create denial with no patient-specific context
        denial_no_context = MagicMock(spec=Denial)
        denial_no_context.denial_text = None
        denial_no_context.plan_context = None
        denial_no_context.health_history = None
        denial_no_context.procedure = "test"
        denial_no_context.diagnosis = "test"

        citations = await MLCitationsHelper.generate_specific_citations(
            denial=denial_no_context
        )

        # Should return empty since no patient-specific context
        assert citations == []

    @pytest.mark.asyncio
    async def test_generate_citations_for_denial_existing_citations(self):
        """Test behavior when citations already exist for the denial."""
        # Setup - denial already has citation context
        denial_with_citations = MagicMock(spec=Denial)
        denial_with_citations.denial_id = 54321
        denial_with_citations.ml_citation_context = [
            "Existing citation 1",
            "Existing citation 2",
        ]

        # Execute
        result = await MLCitationsHelper.generate_citations_for_denial(
            denial_with_citations, speculative=False
        )

        # Verify existing citations were returned
        assert result == ["Existing citation 1", "Existing citation 2"]

    @pytest.mark.asyncio
    @patch("fighthealthinsurance.ml.ml_citations_helper.Denial.objects")
    async def test_generate_citations_for_denial_use_candidate(
        self, mock_denial_objects
    ):
        """Test using candidate citations when they exist with matching procedure/diagnosis."""
        # Setup mock for database update
        mock_queryset = MagicMock()
        mock_queryset.aupdate = AsyncMock()
        mock_denial_objects.filter.return_value = mock_queryset

        # Setup - denial has candidate citations with matching procedure/diagnosis
        denial_with_candidates = MagicMock(spec=Denial)
        denial_with_candidates.denial_id = 66666
        denial_with_candidates.procedure = "Test procedure"
        denial_with_candidates.diagnosis = "Test diagnosis"
        denial_with_candidates.ml_citation_context = None
        denial_with_candidates.candidate_ml_citation_context = [
            "Candidate citation 1",
            "Candidate citation 2",
        ]
        denial_with_candidates.candidate_procedure = "Test procedure"
        denial_with_candidates.candidate_diagnosis = "Test diagnosis"

        # Execute
        result = await MLCitationsHelper.generate_citations_for_denial(
            denial_with_candidates, speculative=False
        )

        # Verify candidate citations were returned
        assert result == ["Candidate citation 1", "Candidate citation 2"]

    @pytest.mark.asyncio
    @patch("fighthealthinsurance.ml.ml_citations_helper.Denial.objects")
    @patch.object(MLCitationsHelper, "_generate_citations_for_denial")
    async def test_generate_citations_for_denial_stores_non_speculative(
        self, mock_generate, mock_denial_objects
    ):
        """Test that generated citations are stored in non-speculative field."""
        # Setup
        mock_generate.return_value = ["Citation A", "Citation B"]
        mock_queryset = MagicMock()
        mock_queryset.aupdate = AsyncMock()
        mock_denial_objects.filter.return_value = mock_queryset

        # Execute
        result = await MLCitationsHelper.generate_citations_for_denial(
            self.mock_denial, speculative=False
        )

        # Verify citations were stored in non-speculative field
        mock_queryset.aupdate.assert_called_once_with(
            ml_citation_context=["Citation A", "Citation B"]
        )
        assert result == ["Citation A", "Citation B"]

    @pytest.mark.asyncio
    @patch("fighthealthinsurance.ml.ml_citations_helper.Denial.objects")
    @patch.object(MLCitationsHelper, "_generate_citations_for_denial")
    async def test_generate_citations_for_denial_stores_speculative(
        self, mock_generate, mock_denial_objects
    ):
        """Test that generated citations are stored in speculative/candidate field."""
        # Setup
        mock_generate.return_value = ["Citation X", "Citation Y"]
        mock_queryset = MagicMock()
        mock_queryset.aupdate = AsyncMock()
        mock_denial_objects.filter.return_value = mock_queryset

        # Execute
        result = await MLCitationsHelper.generate_citations_for_denial(
            self.mock_denial, speculative=True
        )

        # Verify citations were stored in speculative field
        mock_queryset.aupdate.assert_called_once_with(
            candidate_ml_citation_context=["Citation X", "Citation Y"]
        )
        assert result == ["Citation X", "Citation Y"]

    @pytest.mark.asyncio
    @patch("fighthealthinsurance.ml.ml_citations_helper.ml_router")
    async def test_specific_citations_forward_consent_to_backend_selection(
        self, mock_ml_router
    ):
        """Regression: full_find_citation_backends used to be called with NO
        argument, whose default returns [] -- so specific citations were dead
        code. The consent flag must be forwarded so opted-in users get the
        full backends and opted-out users get only internal ones."""
        mock_ml_router.full_find_citation_backends.return_value = []
        await MLCitationsHelper.generate_specific_citations(denial=self.mock_denial)
        mock_ml_router.full_find_citation_backends.assert_called_once_with(
            use_external=False
        )

    @pytest.mark.django_db
    @pytest.mark.asyncio
    @patch("fighthealthinsurance.ml.ml_citations_helper.ml_router")
    async def test_generic_citations_skip_external_backends_when_opted_out(
        self, mock_ml_router
    ):
        """Regression: the generic-citation partial backends are context-only
        EXTERNAL models (Perplexity); a fresh call for a use_external=False
        denial violated the opt-out even though only procedure/diagnosis are
        sent."""
        self.mock_denial.use_external = False
        await MLCitationsHelper.generate_generic_citations(denial=self.mock_denial)
        mock_ml_router.partial_find_citation_backends.assert_not_called()

    @pytest.mark.django_db
    @pytest.mark.asyncio
    @patch("fighthealthinsurance.ml.ml_citations_helper.ml_router")
    async def test_generic_citations_without_denial_default_to_no_external(
        self, mock_ml_router
    ):
        await MLCitationsHelper.generate_generic_citations(
            procedure_opt="pci", diagnosis_opt="coronary artery disease"
        )
        mock_ml_router.partial_find_citation_backends.assert_not_called()

    @pytest.mark.django_db
    @pytest.mark.asyncio
    async def test_supplemental_citations_includes_ecri(self):
        """ECRI guideline citations are appended to supplemental evidence."""
        await ECRIGuideline.objects.acreate(
            guideline_id="test-supp-ecri",
            title="Cardiac Guideline",
            developer_organization="ACC",
            procedure_keywords=["pci"],
            diagnosis_keywords=["coronary artery disease"],
        )

        denial = MagicMock(spec=Denial)
        denial.microsite_slug = None

        result = await MLCitationsHelper._get_supplemental_citations(
            denial=denial,
            procedure="pci",
            diagnosis="coronary artery disease",
        )
        assert any("Cardiac Guideline" in c for c in result)

    @pytest.mark.django_db
    @pytest.mark.asyncio
    async def test_supplemental_citations_empty_when_no_match(self):
        denial = MagicMock(spec=Denial)
        denial.microsite_slug = None

        result = await MLCitationsHelper._get_supplemental_citations(
            denial=denial,
            procedure="totally unmatched procedure",
            diagnosis="totally unmatched diagnosis",
        )
        assert result == []


async def _denial_on_the_row(**fields):
    return await Denial.objects.acreate(
        hashed_email=Denial.get_hashed_email("empty-citations@example.com"),
        denial_text="Denied an MRI.",
        **fields,
    )


async def _column(denial, field):
    return (
        await Denial.objects.filter(denial_id=denial.denial_id)
        .values_list(field, flat=True)
        .aget()
    )


def _generation(**behaviour):
    """Stand in for the generation run (the ML backends and CMS lookup)."""
    return patch.object(
        MLCitationsHelper,
        "_generate_citations_for_denial",
        AsyncMock(**behaviour),
    )


class TestAFinishedEmptyRunIsRecorded:
    """A run that finished and found nothing stores [] rather than leaving
    the column at None, so the appeal step's barrier can tell "done, nothing
    found" (with the citation backend down, every run) from "still running"
    instead of waiting out its timeout. The store keeps the same guards as a
    non-empty one, and never replaces citations another run stored."""

    @pytest.mark.django_db(transaction=True)
    @pytest.mark.asyncio
    async def test_empty_run_stores_an_empty_list(self):
        denial = await _denial_on_the_row()
        with _generation(return_value=[]):
            await MLCitationsHelper.generate_citations_for_denial(
                denial=denial, speculative=False
            )
        assert await _column(denial, "ml_citation_context") == []

    @pytest.mark.django_db(transaction=True)
    @pytest.mark.asyncio
    async def test_empty_speculative_run_stores_an_empty_candidate_list(self):
        denial = await _denial_on_the_row()
        with _generation(return_value=[]):
            await MLCitationsHelper.generate_citations_for_denial(
                denial=denial, speculative=True
            )
        assert await _column(denial, "candidate_ml_citation_context") == []

    @pytest.mark.django_db(transaction=True)
    @pytest.mark.asyncio
    async def test_empty_run_keeps_citations_another_run_stored(self):
        denial = await _denial_on_the_row()
        await Denial.objects.filter(denial_id=denial.denial_id).aupdate(
            ml_citation_context=["Stored by another run"]
        )
        with _generation(return_value=[]):
            await MLCitationsHelper.generate_citations_for_denial(
                denial=denial, speculative=False
            )
        assert await _column(denial, "ml_citation_context") == ["Stored by another run"]

    @pytest.mark.django_db(transaction=True)
    @pytest.mark.asyncio
    async def test_empty_run_for_a_replaced_letter_stores_nothing(self):
        denial = await _denial_on_the_row()
        await Denial.objects.filter(denial_id=denial.denial_id).aupdate(
            denial_text="A different letter."
        )
        with _generation(return_value=[]):
            await MLCitationsHelper.generate_citations_for_denial(
                denial=denial, speculative=False
            )
        assert await _column(denial, "ml_citation_context") is None

    @pytest.mark.django_db(transaction=True)
    @pytest.mark.asyncio
    async def test_empty_run_after_consent_was_withdrawn_stores_nothing(self):
        denial = await _denial_on_the_row(
            health_history="Tried two preventives.", health_history_consent=True
        )

        async def _uses_history_then_refused(denial, timeout, used_history_sink):
            used_history_sink["used"] = True
            await Denial.objects.filter(denial_id=denial.denial_id).aupdate(
                health_history_consent=False
            )
            return []

        with _generation(side_effect=_uses_history_then_refused):
            await MLCitationsHelper.generate_citations_for_denial(
                denial=denial, speculative=False
            )
        assert await _column(denial, "ml_citation_context") is None

    @pytest.mark.django_db(transaction=True)
    @pytest.mark.asyncio
    async def test_run_that_raised_stores_nothing(self):
        denial = await _denial_on_the_row()
        with _generation(side_effect=RuntimeError("backend down")):
            await MLCitationsHelper.generate_citations_for_denial(
                denial=denial, speculative=False
            )
        assert await _column(denial, "ml_citation_context") is None


class TestAFinishedEmptyRunIsTheAnswer:
    """A stored [] is a finished run that found nothing, so the next call
    hands it back rather than generating again. The appeal step's barrier
    releases on that [] and then calls the helper; going again there cost
    the appeal a second full run, which with the backend down found nothing
    again. A speculative [] counts only while it was found for the live
    procedure and diagnosis, like a non-empty one."""

    @pytest.mark.django_db(transaction=True)
    @pytest.mark.asyncio
    async def test_stored_empty_list_is_returned_without_generating(self):
        denial = await _denial_on_the_row(ml_citation_context=[])
        with _generation(return_value=["Generated again"]) as generation:
            result = await MLCitationsHelper.generate_citations_for_denial(
                denial=denial, speculative=False
            )
        assert (result, generation.await_count) == ([], 0)

    @pytest.mark.django_db(transaction=True)
    @pytest.mark.asyncio
    async def test_empty_candidate_list_for_the_same_details_is_used_without_generating(
        self,
    ):
        denial = await _denial_on_the_row(
            procedure="MRI",
            diagnosis="Migraine",
            candidate_procedure="MRI",
            candidate_diagnosis="Migraine",
            candidate_ml_citation_context=[],
        )
        with _generation(return_value=["Generated again"]) as generation:
            result = await MLCitationsHelper.generate_citations_for_denial(
                denial=denial, speculative=False
            )
        assert (result, generation.await_count) == ([], 0)

    @pytest.mark.django_db(transaction=True)
    @pytest.mark.asyncio
    async def test_empty_candidate_list_for_another_procedure_is_generated_again(
        self,
    ):
        denial = await _denial_on_the_row(
            procedure="Knee MRI",
            diagnosis="Migraine",
            candidate_procedure="MRI",
            candidate_diagnosis="Migraine",
            candidate_ml_citation_context=[],
        )
        with _generation(return_value=["Cited for the knee MRI"]) as generation:
            result = await MLCitationsHelper.generate_citations_for_denial(
                denial=denial, speculative=False
            )
        assert (result, generation.await_count) == (["Cited for the knee MRI"], 1)
