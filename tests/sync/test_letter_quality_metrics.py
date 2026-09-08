"""letter_quality_metrics: draft quality per backend on /metrics."""

import datetime

from django.test import TestCase
from django.utils import timezone

from fighthealthinsurance.letter_quality_metrics import (
    LetterQualityCollector,
    quality_by_model,
)
from fighthealthinsurance.ml import letter_quality
from fighthealthinsurance.ml.model_identity import legacy_unresolved_label
from fighthealthinsurance.models import Denial, ProposedAppeal


class LetterQualityMetricsTest(TestCase):
    def setUp(self):
        self.denial = Denial.objects.create(hashed_email="h", denial_text="denied")

    def _draft(self, model_name, quality, grounding, days_ago=0, scorer=None):
        return ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text=f"draft {model_name} {quality} {days_ago} {scorer}",
            model_name=model_name,
            quality_score=quality,
            grounding_score=grounding,
            quality_scorer=scorer or letter_quality.SCORER,
            quality_scored_at=timezone.now() - datetime.timedelta(days=days_ago),
        )

    def test_aggregates_per_model_inside_the_window(self):
        self._draft("m1", 0.8, 2)
        self._draft("m1", 0.4, 0)
        self._draft("m2", 1.0, 2)
        self._draft("m1", 0.0, 0, days_ago=3)  # outside the 24h window
        ProposedAppeal.objects.create(  # unscored: invisible here
            for_denial=self.denial, appeal_text="unscored", model_name="m1"
        )
        stats = quality_by_model(timezone.now())
        key = ("m1", letter_quality.SCORER)
        self.assertAlmostEqual(stats[key]["avg"], 0.6)
        self.assertEqual(stats[key]["scored"], 2)
        self.assertEqual(stats[key]["ungrounded"], 1)
        self.assertEqual(stats[("m2", letter_quality.SCORER)]["ungrounded"], 0)

    def test_two_stored_spellings_of_one_backend_are_one_series(self):
        """Legacy rows hold object reprs with a memory address; every address
        used to be its own Prometheus series (review). They collapse to the
        class, and the merge is a scored-weighted mean with summed counts."""
        cls = "fighthealthinsurance.ml.ml_models.RemoteFullOpenLike"
        self._draft(f"<{cls} object at 0x7f0000000010>", 0.8, 2)
        self._draft(f"<{cls} object at 0x7f0000000020>", 0.2, 0)
        self._draft(f"<{cls} object at 0x7f0000000030>", 0.2, 0)
        stats = quality_by_model(timezone.now())
        key = (legacy_unresolved_label("RemoteFullOpenLike"), letter_quality.SCORER)
        self.assertEqual(list(stats), [key])
        self.assertAlmostEqual(stats[key]["avg"], (0.8 + 0.2 + 0.2) / 3)
        self.assertEqual(stats[key]["scored"], 3)
        self.assertEqual(stats[key]["ungrounded"], 2)

    def test_padded_names_merge_and_blank_names_are_skipped(self):
        self._draft(" m1 ", 1.0, 2)
        self._draft("m1", 0.0, 2)
        self._draft("   ", 0.5, 2)
        stats = quality_by_model(timezone.now())
        self.assertEqual(list(stats), [("m1", letter_quality.SCORER)])
        self.assertAlmostEqual(stats[("m1", letter_quality.SCORER)]["avg"], 0.5)
        self.assertEqual(stats[("m1", letter_quality.SCORER)]["scored"], 2)

    def test_each_scorer_is_its_own_series_and_old_rubrics_are_dropped(self):
        self._draft("m1", 0.8, 2)
        self._draft("m1", 0.0, 0, scorer="typesafe/other/rubric-0")
        repointed = f"typesafe/other/rubric-{letter_quality.RUBRIC_VERSION}"
        self._draft("m1", 0.6, 2, scorer=repointed)
        stats = quality_by_model(timezone.now())
        self.assertAlmostEqual(stats[("m1", letter_quality.SCORER)]["avg"], 0.8)
        self.assertAlmostEqual(stats[("m1", repointed)]["avg"], 0.6)
        self.assertEqual(len(stats), 2, "rubric-0 rows are not a series")

    def test_collector_emits_labelled_samples(self):
        self._draft("m1", 0.5, 1)
        families = {m.name: m for m in LetterQualityCollector().collect()}
        avg = families["fhi_letter_quality_avg"]
        self.assertEqual(
            [(s.labels["model_name"], s.labels["scorer"], s.value) for s in avg.samples],
            [("m1", letter_quality.SCORER, 0.5)],
        )
        self.assertIn("fhi_letter_quality_requests", families)

    def test_collector_never_raises(self):
        with self.settings():
            from unittest.mock import patch

            with patch(
                "fighthealthinsurance.letter_quality_metrics.quality_by_model",
                side_effect=RuntimeError("db away"),
            ):
                families = list(LetterQualityCollector().collect())
        self.assertEqual(len(families), 4)
