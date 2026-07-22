"""Tests for the hardened generic ML caches (citations + questions).

Covers the behaviors added with the (procedure, diagnosis, version)
uniqueness work:

* the versioned upsert is safe when parked/deprecated duplicate rows exist
  (the pre-constraint failure mode was ``MultipleObjectsReturned`` breaking
  the cache write forever for that pair);
* the TTL: stale rows (old or pre-TTL ``updated_at``) trigger regeneration
  and are refreshed in place;
* stale content is served as a fallback when regeneration returns nothing,
  without refreshing the timestamp;
* unreasonable ML output is returned to the caller but never cached;
* rows at other cache versions are invisible to reads.
"""

import datetime

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from django.utils import timezone

from fighthealthinsurance.ml.ml_appeal_questions_helper import (
    MLAppealQuestionsHelper,
    _questions_worth_caching,
)
from fighthealthinsurance.ml.ml_citations_helper import (
    MLCitationsHelper,
    _citations_worth_caching,
)
from fighthealthinsurance.models import (
    GenericContextGeneration,
    GenericQuestionGeneration,
)

# Unique to this module: async-unit DB tests don't get transaction rollback
# (async ORM writes commit), so tests must use their own rows and clean them
# up explicitly — see _clear_cache_rows and test_generic_model_integration.py
# for the same convention.
PROCEDURE = "cache hardening procedure"
DIAGNOSIS = "cache hardening diagnosis"
GOOD_CITATIONS = [
    "Smith J et al. (2025). Widget install outcomes. J Widgetry.",
    "Doe A (2026). Widget deficiency treatment guidelines. NEJM.",
]
GOOD_QUESTIONS = [("What documentation supports medical necessity?", "")]
STALE = timezone.now() - datetime.timedelta(days=45)


async def _clear_cache_rows() -> None:
    """Delete this module's cache rows; called at the start of every DB test
    because committed rows from a previous test would otherwise leak in."""
    await GenericContextGeneration.objects.filter(
        procedure=PROCEDURE, diagnosis=DIAGNOSIS
    ).adelete()
    await GenericQuestionGeneration.objects.filter(
        procedure=PROCEDURE, diagnosis=DIAGNOSIS
    ).adelete()


def _mock_citation_router(mock_router):
    backend = MagicMock()
    # get_citations is only *called* (never awaited — best_within_timelimit is
    # mocked in these tests), so a plain MagicMock records the kwargs for the
    # patient-context-free assertions without creating never-awaited
    # coroutines that emit RuntimeWarnings.
    backend.get_citations = MagicMock(return_value=[])
    mock_router.partial_find_citation_backends.return_value = [backend]
    return backend


async def _generate_citations():
    return await MLCitationsHelper.generate_generic_citations(
        procedure_opt=PROCEDURE, diagnosis_opt=DIAGNOSIS
    )


class TestCitationCacheHardening:
    @pytest.mark.django_db
    @pytest.mark.asyncio
    @patch("fighthealthinsurance.ml.ml_citations_helper.ml_router")
    async def test_upsert_is_safe_with_parked_duplicate_rows(self, mock_router):
        """Parked rows (version=-pk, as the dedupe migration leaves them) must
        not break reads or the upsert for the current version."""
        await _clear_cache_rows()
        parked_a = await GenericContextGeneration.objects.acreate(
            procedure=PROCEDURE,
            diagnosis=DIAGNOSIS,
            generated_context=["old dup a"],
        )
        parked_a.version = -parked_a.id
        await parked_a.asave(update_fields=["version"])
        parked_b = await GenericContextGeneration.objects.acreate(
            procedure=PROCEDURE,
            diagnosis=DIAGNOSIS,
            generated_context=["old dup b"],
        )
        parked_b.version = -parked_b.id
        await parked_b.asave(update_fields=["version"])

        backend = _mock_citation_router(mock_router)
        with patch(
            "fighthealthinsurance.ml.ml_citations_helper.best_within_timelimit",
            new_callable=AsyncMock,
            return_value=list(GOOD_CITATIONS),
        ):
            result = await _generate_citations()

        assert result == GOOD_CITATIONS
        # The generic (cross-patient) path must never hand backends any
        # patient-specific context — only procedure/diagnosis.
        backend.get_citations.assert_called_once_with(
            denial_text=None,
            procedure=PROCEDURE,
            diagnosis=DIAGNOSIS,
            patient_context=None,
            plan_context=None,
        )
        current = await GenericContextGeneration.objects.filter(
            procedure=PROCEDURE,
            diagnosis=DIAGNOSIS,
            version=GenericContextGeneration.CURRENT_VERSION,
        ).afirst()
        assert current is not None
        assert current.generated_context == GOOD_CITATIONS
        # Parked rows are retained untouched.
        assert (
            await GenericContextGeneration.objects.filter(
                procedure=PROCEDURE, diagnosis=DIAGNOSIS
            ).acount()
            == 3
        )

    @pytest.mark.django_db
    @pytest.mark.asyncio
    @patch("fighthealthinsurance.ml.ml_citations_helper.ml_router")
    async def test_stale_row_triggers_regeneration_and_refresh(self, mock_router):
        await _clear_cache_rows()
        row = await GenericContextGeneration.objects.acreate(
            procedure=PROCEDURE,
            diagnosis=DIAGNOSIS,
            generated_context=["Old cached citation from a previous generation."],
            updated_at=STALE,
        )

        _mock_citation_router(mock_router)
        with patch(
            "fighthealthinsurance.ml.ml_citations_helper.best_within_timelimit",
            new_callable=AsyncMock,
            return_value=list(GOOD_CITATIONS),
        ) as mock_generate:
            result = await _generate_citations()

        assert mock_generate.await_count == 1
        assert result == GOOD_CITATIONS
        await row.arefresh_from_db()
        assert row.generated_context == GOOD_CITATIONS
        assert row.updated_at is not None and row.updated_at > STALE
        # Refreshed in place — no second row.
        assert (
            await GenericContextGeneration.objects.filter(
                procedure=PROCEDURE, diagnosis=DIAGNOSIS
            ).acount()
            == 1
        )

    @pytest.mark.django_db
    @pytest.mark.asyncio
    @patch("fighthealthinsurance.ml.ml_citations_helper.ml_router")
    async def test_pre_ttl_row_without_updated_at_is_stale(self, mock_router):
        """Rows that predate the TTL field (updated_at=None) count as stale."""
        await _clear_cache_rows()
        await GenericContextGeneration.objects.acreate(
            procedure=PROCEDURE,
            diagnosis=DIAGNOSIS,
            generated_context=["Pre-TTL cached citation, no timestamp."],
        )

        _mock_citation_router(mock_router)
        with patch(
            "fighthealthinsurance.ml.ml_citations_helper.best_within_timelimit",
            new_callable=AsyncMock,
            return_value=list(GOOD_CITATIONS),
        ) as mock_generate:
            result = await _generate_citations()

        assert mock_generate.await_count == 1
        assert result == GOOD_CITATIONS

    @pytest.mark.django_db
    @pytest.mark.asyncio
    @patch("fighthealthinsurance.ml.ml_citations_helper.ml_router")
    async def test_stale_content_served_when_regeneration_empty(self, mock_router):
        await _clear_cache_rows()
        stale_content = ["Old but still useful cached citation text here."]
        row = await GenericContextGeneration.objects.acreate(
            procedure=PROCEDURE,
            diagnosis=DIAGNOSIS,
            generated_context=list(stale_content),
            updated_at=STALE,
        )

        _mock_citation_router(mock_router)
        with patch(
            "fighthealthinsurance.ml.ml_citations_helper.best_within_timelimit",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await _generate_citations()

        assert result == stale_content
        # Timestamp untouched so the next request retries generation.
        await row.arefresh_from_db()
        assert row.updated_at == STALE

    @pytest.mark.django_db
    @pytest.mark.asyncio
    @patch("fighthealthinsurance.ml.ml_citations_helper.ml_router")
    async def test_stale_content_served_over_junk_regeneration(self, mock_router):
        """When a stale row exists and regeneration returns junk (fails the
        cache guard), the known-good stale content wins — otherwise junk
        would be served over good content on every request forever."""
        await _clear_cache_rows()
        stale_content = ["Old but still useful cached citation text here."]
        row = await GenericContextGeneration.objects.acreate(
            procedure=PROCEDURE,
            diagnosis=DIAGNOSIS,
            generated_context=list(stale_content),
            updated_at=STALE,
        )

        _mock_citation_router(mock_router)
        with patch(
            "fighthealthinsurance.ml.ml_citations_helper.best_within_timelimit",
            new_callable=AsyncMock,
            return_value=["N/A"],
        ):
            result = await _generate_citations()

        assert result == stale_content
        await row.arefresh_from_db()
        # Junk was neither cached nor allowed to refresh the timestamp.
        assert row.generated_context == stale_content
        assert row.updated_at == STALE

    @pytest.mark.django_db
    @pytest.mark.asyncio
    @patch("fighthealthinsurance.ml.ml_citations_helper.ml_router")
    async def test_junk_result_returned_but_not_cached(self, mock_router):
        await _clear_cache_rows()
        _mock_citation_router(mock_router)
        with patch(
            "fighthealthinsurance.ml.ml_citations_helper.best_within_timelimit",
            new_callable=AsyncMock,
            return_value=["N/A"],
        ):
            result = await _generate_citations()

        # The caller still gets the generation, but nothing was pinned.
        assert result == ["N/A"]
        assert (
            await GenericContextGeneration.objects.filter(
                procedure=PROCEDURE, diagnosis=DIAGNOSIS
            ).acount()
            == 0
        )

    @pytest.mark.django_db
    @pytest.mark.asyncio
    @patch("fighthealthinsurance.ml.ml_citations_helper.ml_router")
    async def test_denial_entry_point_does_not_leak_patient_context(self, mock_router):
        """Even when called WITH a denial full of patient data, the generic
        (cross-patient) path must pass none of it to the backends.

        The denial=None entry point can't catch a conditional leak like
        ``denial_text=denial.denial_text if denial else None`` — this can.
        """
        await _clear_cache_rows()
        from fighthealthinsurance.models import Denial

        denial = MagicMock(spec=Denial)
        denial.procedure = PROCEDURE
        denial.diagnosis = DIAGNOSIS
        denial.denial_text = "SENSITIVE denial text"
        denial.health_history = "SENSITIVE health history"
        denial.plan_context = "SENSITIVE plan context"
        denial.microsite_slug = None

        backend = _mock_citation_router(mock_router)
        with patch(
            "fighthealthinsurance.ml.ml_citations_helper.best_within_timelimit",
            new_callable=AsyncMock,
            return_value=list(GOOD_CITATIONS),
        ):
            await MLCitationsHelper.generate_generic_citations(denial=denial)

        backend.get_citations.assert_called_once_with(
            denial_text=None,
            procedure=PROCEDURE,
            diagnosis=DIAGNOSIS,
            patient_context=None,
            plan_context=None,
        )
        # And nothing sensitive landed in the cached row either.
        cached = await GenericContextGeneration.objects.filter(
            procedure=PROCEDURE,
            diagnosis=DIAGNOSIS,
            version=GenericContextGeneration.CURRENT_VERSION,
        ).afirst()
        assert cached is not None
        assert "SENSITIVE" not in str(cached.generated_context)

    @pytest.mark.django_db
    @pytest.mark.asyncio
    @patch("fighthealthinsurance.ml.ml_citations_helper.ml_router")
    async def test_rows_at_other_versions_are_invisible(self, mock_router):
        """A row at a non-current version (e.g. after a CURRENT_VERSION bump)
        is treated as a miss."""
        await _clear_cache_rows()
        await GenericContextGeneration.objects.acreate(
            procedure=PROCEDURE,
            diagnosis=DIAGNOSIS,
            generated_context=["Citation from an old cache generation."],
            updated_at=timezone.now(),
            version=-5,
        )

        _mock_citation_router(mock_router)
        with patch(
            "fighthealthinsurance.ml.ml_citations_helper.best_within_timelimit",
            new_callable=AsyncMock,
            return_value=list(GOOD_CITATIONS),
        ) as mock_generate:
            result = await _generate_citations()

        assert mock_generate.await_count == 1
        assert result == GOOD_CITATIONS


class TestQuestionCacheHardening:
    def _mock_question_model(self, mock_full, mock_partial, questions):
        model = AsyncMock()
        model.get_appeal_questions = AsyncMock(return_value=questions)
        model.quality = MagicMock(return_value=50)
        mock_full.return_value = [model]
        mock_partial.return_value = [model]
        return model

    @pytest.mark.django_db
    @pytest.mark.asyncio
    @patch(
        "fighthealthinsurance.ml.ml_appeal_questions_helper.ml_router.partial_qa_backends"
    )
    @patch(
        "fighthealthinsurance.ml.ml_appeal_questions_helper.ml_router.full_qa_backends"
    )
    async def test_upsert_is_safe_with_parked_duplicate_rows(
        self, mock_full, mock_partial
    ):
        await _clear_cache_rows()
        parked = await GenericQuestionGeneration.objects.acreate(
            procedure=PROCEDURE,
            diagnosis=DIAGNOSIS,
            generated_questions=[["Old dup question?", ""]],
        )
        parked.version = -parked.id
        await parked.asave(update_fields=["version"])

        model = self._mock_question_model(mock_full, mock_partial, GOOD_QUESTIONS)
        result = await MLAppealQuestionsHelper.generate_generic_questions(
            procedure=PROCEDURE, diagnosis=DIAGNOSIS
        )

        assert result == [(q, "") for q, _ in GOOD_QUESTIONS]
        model.get_appeal_questions.assert_called_once()
        # The models must never receive patient context on the generic path:
        # the shared cache is cross-patient.
        call_kwargs = model.get_appeal_questions.call_args.kwargs
        assert call_kwargs.get("denial_text") is None
        assert "patient_context" not in call_kwargs

        current = await GenericQuestionGeneration.objects.filter(
            procedure=PROCEDURE,
            diagnosis=DIAGNOSIS,
            version=GenericQuestionGeneration.CURRENT_VERSION,
        ).afirst()
        assert current is not None
        assert (
            await GenericQuestionGeneration.objects.filter(
                procedure=PROCEDURE, diagnosis=DIAGNOSIS
            ).acount()
            == 2
        )

    @pytest.mark.django_db
    @pytest.mark.asyncio
    @patch(
        "fighthealthinsurance.ml.ml_appeal_questions_helper.ml_router.partial_qa_backends"
    )
    @patch(
        "fighthealthinsurance.ml.ml_appeal_questions_helper.ml_router.full_qa_backends"
    )
    async def test_stale_row_triggers_regeneration_and_refresh(
        self, mock_full, mock_partial
    ):
        await _clear_cache_rows()
        row = await GenericQuestionGeneration.objects.acreate(
            procedure=PROCEDURE,
            diagnosis=DIAGNOSIS,
            generated_questions=[["Old cached question from before?", ""]],
            updated_at=STALE,
        )

        model = self._mock_question_model(mock_full, mock_partial, GOOD_QUESTIONS)
        result = await MLAppealQuestionsHelper.generate_generic_questions(
            procedure=PROCEDURE, diagnosis=DIAGNOSIS
        )

        model.get_appeal_questions.assert_called_once()
        assert result == [(q, "") for q, _ in GOOD_QUESTIONS]
        await row.arefresh_from_db()
        assert row.updated_at is not None and row.updated_at > STALE

    @pytest.mark.django_db
    @pytest.mark.asyncio
    @patch(
        "fighthealthinsurance.ml.ml_appeal_questions_helper.ml_router.partial_qa_backends"
    )
    @patch(
        "fighthealthinsurance.ml.ml_appeal_questions_helper.ml_router.full_qa_backends"
    )
    async def test_stale_content_served_when_regeneration_empty(
        self, mock_full, mock_partial
    ):
        await _clear_cache_rows()
        stale_questions = [["Is the old cached question still shown?", ""]]
        row = await GenericQuestionGeneration.objects.acreate(
            procedure=PROCEDURE,
            diagnosis=DIAGNOSIS,
            generated_questions=stale_questions,
            updated_at=STALE,
        )

        self._mock_question_model(mock_full, mock_partial, [])
        result = await MLAppealQuestionsHelper.generate_generic_questions(
            procedure=PROCEDURE, diagnosis=DIAGNOSIS
        )

        assert result == stale_questions
        await row.arefresh_from_db()
        assert row.updated_at == STALE

    @pytest.mark.django_db
    @pytest.mark.asyncio
    @patch(
        "fighthealthinsurance.ml.ml_appeal_questions_helper.ml_router.partial_qa_backends"
    )
    @patch(
        "fighthealthinsurance.ml.ml_appeal_questions_helper.ml_router.full_qa_backends"
    )
    async def test_stale_questions_served_over_junk_regeneration(
        self, mock_full, mock_partial
    ):
        """Junk regeneration (fails the cache guard) must not displace
        known-good stale cached questions."""
        await _clear_cache_rows()
        stale_questions = [["Is the old cached question still shown?", ""]]
        row = await GenericQuestionGeneration.objects.acreate(
            procedure=PROCEDURE,
            diagnosis=DIAGNOSIS,
            generated_questions=stale_questions,
            updated_at=STALE,
        )

        self._mock_question_model(mock_full, mock_partial, [("Bad", "")])
        result = await MLAppealQuestionsHelper.generate_generic_questions(
            procedure=PROCEDURE, diagnosis=DIAGNOSIS
        )

        assert result == stale_questions
        await row.arefresh_from_db()
        assert row.generated_questions == stale_questions
        assert row.updated_at == STALE

    @pytest.mark.django_db
    @pytest.mark.asyncio
    @patch(
        "fighthealthinsurance.ml.ml_appeal_questions_helper.ml_router.partial_qa_backends"
    )
    @patch(
        "fighthealthinsurance.ml.ml_appeal_questions_helper.ml_router.full_qa_backends"
    )
    async def test_junk_questions_returned_but_not_cached(
        self, mock_full, mock_partial
    ):
        await _clear_cache_rows()
        # Short and not question-shaped: fails _questions_worth_caching.
        junk = [("Bad", "")]
        self._mock_question_model(mock_full, mock_partial, junk)
        result = await MLAppealQuestionsHelper.generate_generic_questions(
            procedure=PROCEDURE, diagnosis=DIAGNOSIS
        )

        assert result == [("Bad", "")]
        assert (
            await GenericQuestionGeneration.objects.filter(
                procedure=PROCEDURE, diagnosis=DIAGNOSIS
            ).acount()
            == 0
        )


class TestWorthCachingGuards:
    """Direct unit coverage of the reasonable-response guards."""

    def test_citations_guard_accepts_realistic_result(self):
        assert _citations_worth_caching(GOOD_CITATIONS) is True

    def test_citations_guard_rejects_empty_and_blank(self):
        assert _citations_worth_caching([]) is False
        assert _citations_worth_caching(["   "]) is False

    def test_citations_guard_rejects_all_short_entries(self):
        assert _citations_worth_caching(["N/A", "none found"]) is False

    def test_citations_guard_rejects_non_string_entries(self):
        assert _citations_worth_caching([{"title": "x"}, GOOD_CITATIONS[0]]) is False  # type: ignore[list-item]

    def test_questions_guard_accepts_realistic_result(self):
        assert _questions_worth_caching(GOOD_QUESTIONS) is True

    def test_questions_guard_rejects_empty_short_and_non_questions(self):
        assert _questions_worth_caching([]) is False
        assert _questions_worth_caching([("Bad", "")]) is False
        # Non-trivial text but nothing that reads as a question.
        assert (
            _questions_worth_caching([("This is a statement not a question", "")])
            is False
        )

    def test_questions_guard_rejects_oversized_result(self):
        many = [(f"Is this necessary question number {i}?", "") for i in range(11)]
        assert _questions_worth_caching(many) is False

    def test_questions_guard_rejects_malformed_entries(self):
        assert _questions_worth_caching([None]) is False  # type: ignore[list-item]
        assert _questions_worth_caching([(123, "")]) is False  # type: ignore[list-item]
