"""Tests for CMS coverage integration in MLCitationsHelper.

These verify that:
- The DB cache short-circuits the network call when fresh entries exist.
- Stale cache entries trigger a refetch.
- The Medicare-plan path reads/writes the medicare_citations field, while
  commercial plans use generic_citations.
- _generate_citations_for_denial appends CMS citations alongside ML
  citations and dedupes them.
"""

import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django.utils import timezone

from fighthealthinsurance.ml.ml_citations_helper import MLCitationsHelper
from fighthealthinsurance.models import Denial


class TestMLCitationsHelperCMSIntegration:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.mock_denial = MagicMock(spec=Denial)
        self.mock_denial.pk = None  # _denial_is_medicare_plan short-circuits
        self.mock_denial.denial_id = 12345
        self.mock_denial.procedure = "MRI"
        self.mock_denial.diagnosis = "Headache"
        self.mock_denial.denial_text = "Test denial text"
        self.mock_denial.health_history = "Test history"
        self.mock_denial.plan_context = ""
        self.mock_denial.ml_citation_context = None
        self.mock_denial.candidate_ml_citation_context = None
        self.mock_denial.candidate_procedure = None
        self.mock_denial.candidate_diagnosis = None
        self.mock_denial.microsite_slug = None
        self.mock_denial.use_external = False

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_procedure_or_diagnosis(self):
        denial = MagicMock(spec=Denial)
        denial.pk = None
        denial.procedure = None
        denial.diagnosis = None
        result = await MLCitationsHelper.generate_cms_coverage_citations(denial=denial)
        assert result == []

    @pytest.mark.asyncio
    @patch("fighthealthinsurance.ml.ml_citations_helper.CMSCoverageCache")
    @patch("fighthealthinsurance.ml.ml_citations_helper.get_cms_coverage_citations")
    async def test_uses_fresh_cache_when_available(self, mock_fetch, mock_cache_cls):
        # Simulate a fresh DB cache hit on the variant-specific timestamp
        cache_entry = MagicMock()
        cache_entry.generic_citations = ["Cached NCD A", "Cached NCD B"]
        cache_entry.medicare_citations = []
        cache_entry.generic_updated_at = timezone.now()
        cache_entry.medicare_updated_at = None

        mock_qs = MagicMock()
        mock_qs.afirst = AsyncMock(return_value=cache_entry)
        mock_cache_cls.objects.filter.return_value = mock_qs
        mock_cache_cls.objects.aupdate_or_create = AsyncMock()

        result = await MLCitationsHelper.generate_cms_coverage_citations(
            denial=self.mock_denial
        )

        assert result == ["Cached NCD A", "Cached NCD B"]
        mock_fetch.assert_not_called()
        mock_cache_cls.objects.aupdate_or_create.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("fighthealthinsurance.ml.ml_citations_helper.CMSCoverageCache")
    @patch("fighthealthinsurance.ml.ml_citations_helper.get_cms_coverage_citations")
    async def test_refetches_when_cache_is_stale(self, mock_fetch, mock_cache_cls):
        cache_entry = MagicMock()
        cache_entry.pk = 7
        cache_entry.generic_citations = ["Stale citation"]
        cache_entry.medicare_citations = []
        cache_entry.generic_updated_at = timezone.now() - datetime.timedelta(days=60)
        cache_entry.medicare_updated_at = None

        mock_qs = MagicMock()
        mock_qs.afirst = AsyncMock(return_value=cache_entry)
        mock_cache_cls.objects.filter.return_value = mock_qs
        mock_cache_cls.objects.aupdate_or_create = AsyncMock(
            return_value=(cache_entry, False)
        )

        mock_fetch.return_value = ["Fresh citation"]

        result = await MLCitationsHelper.generate_cms_coverage_citations(
            denial=self.mock_denial
        )

        assert result == ["Fresh citation"]
        mock_fetch.assert_awaited_once()
        mock_cache_cls.objects.aupdate_or_create.assert_awaited_once()
        kwargs = mock_cache_cls.objects.aupdate_or_create.await_args.kwargs
        assert kwargs["procedure"] == "mri"
        assert kwargs["diagnosis"] == "headache"
        assert "generic_citations" in kwargs["defaults"]
        assert "generic_updated_at" in kwargs["defaults"]

    @pytest.mark.asyncio
    @patch("fighthealthinsurance.ml.ml_citations_helper.CMSCoverageCache")
    @patch("fighthealthinsurance.ml.ml_citations_helper.get_cms_coverage_citations")
    async def test_refetches_when_other_variant_is_fresh_but_ours_is_missing(
        self, mock_fetch, mock_cache_cls
    ):
        # Medicare side was just refreshed, but generic side has never been
        # populated. We're a non-Medicare denial, so we must refetch.
        cache_entry = MagicMock()
        cache_entry.pk = 9
        cache_entry.generic_citations = []
        cache_entry.medicare_citations = ["Some Medicare citation"]
        cache_entry.generic_updated_at = None
        cache_entry.medicare_updated_at = timezone.now()

        mock_qs = MagicMock()
        mock_qs.afirst = AsyncMock(return_value=cache_entry)
        mock_cache_cls.objects.filter.return_value = mock_qs
        mock_cache_cls.objects.aupdate_or_create = AsyncMock(
            return_value=(cache_entry, False)
        )
        mock_fetch.return_value = ["Fresh generic citation"]

        result = await MLCitationsHelper.generate_cms_coverage_citations(
            denial=self.mock_denial
        )

        assert result == ["Fresh generic citation"]
        mock_fetch.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("fighthealthinsurance.ml.ml_citations_helper.CMSCoverageCache")
    @patch("fighthealthinsurance.ml.ml_citations_helper.get_cms_coverage_citations")
    async def test_creates_cache_entry_when_none_exists(
        self, mock_fetch, mock_cache_cls
    ):
        mock_qs = MagicMock()
        mock_qs.afirst = AsyncMock(return_value=None)
        mock_cache_cls.objects.filter.return_value = mock_qs
        mock_cache_cls.objects.aupdate_or_create = AsyncMock(
            return_value=(MagicMock(), True)
        )

        mock_fetch.return_value = ["New citation"]

        result = await MLCitationsHelper.generate_cms_coverage_citations(
            denial=self.mock_denial
        )

        assert result == ["New citation"]
        mock_cache_cls.objects.aupdate_or_create.assert_awaited_once()
        kwargs = mock_cache_cls.objects.aupdate_or_create.await_args.kwargs
        assert kwargs["procedure"] == "mri"
        assert kwargs["diagnosis"] == "headache"
        assert kwargs["defaults"]["generic_citations"] == ["New citation"]
        assert kwargs["defaults"].get("generic_updated_at") is not None

    @pytest.mark.asyncio
    @patch("fighthealthinsurance.ml.ml_citations_helper.CMSCoverageCache")
    @patch("fighthealthinsurance.ml.ml_citations_helper.get_cms_coverage_citations")
    async def test_does_not_persist_when_fetch_returns_empty(
        self, mock_fetch, mock_cache_cls
    ):
        mock_qs = MagicMock()
        mock_qs.afirst = AsyncMock(return_value=None)
        mock_cache_cls.objects.filter.return_value = mock_qs
        mock_cache_cls.objects.aupdate_or_create = AsyncMock()
        mock_fetch.return_value = []

        result = await MLCitationsHelper.generate_cms_coverage_citations(
            denial=self.mock_denial
        )

        assert result == []
        mock_cache_cls.objects.aupdate_or_create.assert_not_awaited()

    @pytest.mark.asyncio
    @patch.object(
        MLCitationsHelper, "generate_cms_coverage_citations", new_callable=AsyncMock
    )
    @patch.object(
        MLCitationsHelper, "generate_specific_citations", new_callable=AsyncMock
    )
    @patch.object(
        MLCitationsHelper, "generate_generic_citations", new_callable=AsyncMock
    )
    @patch("fighthealthinsurance.ml.ml_citations_helper.best_within_timelimit")
    async def test_underscore_helper_appends_cms_citations(
        self, mock_best, mock_generic, mock_specific, mock_cms
    ):
        mock_best.return_value = ["ML citation 1"]
        mock_generic.return_value = ["ML citation 1"]
        mock_specific.return_value = ["ML citation 1"]
        mock_cms.return_value = ["CMS NCD 220.5"]

        result = await MLCitationsHelper._generate_citations_for_denial(
            denial=self.mock_denial, timeout=30
        )

        assert "ML citation 1" in result
        assert "CMS NCD 220.5" in result

    @pytest.mark.asyncio
    @patch.object(
        MLCitationsHelper, "generate_cms_coverage_citations", new_callable=AsyncMock
    )
    @patch.object(
        MLCitationsHelper, "generate_specific_citations", new_callable=AsyncMock
    )
    @patch.object(
        MLCitationsHelper, "generate_generic_citations", new_callable=AsyncMock
    )
    @patch("fighthealthinsurance.ml.ml_citations_helper.best_within_timelimit")
    async def test_underscore_helper_dedupes_overlap(
        self, mock_best, mock_generic, mock_specific, mock_cms
    ):
        shared = "Shared citation"
        mock_best.return_value = [shared]
        mock_generic.return_value = [shared]
        mock_specific.return_value = [shared]
        mock_cms.return_value = [shared, "Unique CMS citation"]

        result = await MLCitationsHelper._generate_citations_for_denial(
            denial=self.mock_denial, timeout=30
        )

        assert result.count(shared) == 1
        assert "Unique CMS citation" in result

    @pytest.mark.asyncio
    @patch.object(
        MLCitationsHelper, "generate_cms_coverage_citations", new_callable=AsyncMock
    )
    @patch.object(
        MLCitationsHelper, "generate_generic_citations", new_callable=AsyncMock
    )
    async def test_underscore_helper_short_circuits_with_cms_only(
        self, mock_generic, mock_cms
    ):
        # Denial has no text/history, so only generic ML path runs
        denial = MagicMock(spec=Denial)
        denial.pk = None
        denial.denial_id = 99
        denial.procedure = "MRI"
        denial.diagnosis = "Headache"
        denial.denial_text = ""
        denial.health_history = ""
        denial.ml_citation_context = None

        mock_generic.return_value = []
        mock_cms.return_value = ["CMS NCD only"]

        result = await MLCitationsHelper._generate_citations_for_denial(
            denial=denial, timeout=30
        )

        assert result == ["CMS NCD only"]
