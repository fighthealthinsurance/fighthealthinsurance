"""Stored drafts are replayed newest-first and capped, not dumped in full.

Rows accumulate per denial across retries, reconnects and re-runs of the same
denial. The replay had no cap, so a denial that had been generated against a few
times opened with a wall of stored letters and buried the fresh draft at the
bottom. Melanie hit exactly this on prod: eighteen stored drafts, then one new
one, last.

These pin the cap and the ordering of what is SHOWN. They are deliberately
independent of TypeSafe scoring: with the ranking flags off there are no scores
to sort by, and the page still must not open with a haystack.

What the cap does not do, on purpose: rows it holds back stay available to the
rest of the run. Synthesis still draws on every stored draft, because it is
choosing inputs rather than showing them; and if the model regenerates text
identical to a held-back row, the uniqueness handler streams that stored row,
which is a draft the user has not seen this session and is the right outcome.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import async_to_sync
from django.test import TestCase, override_settings

from fighthealthinsurance.common_view_logic import AppealsBackendHelper
from fighthealthinsurance.models import Denial, ProposedAppeal


# Temporal off, explicitly. The interactive path records a durable intake
# intent and tries an inline delivery to the Temporal cluster when the three
# flags are on; they are read from the environment at settings import, so an
# ambient TEMPORAL_ENABLED=true would make these tests open a connection to a
# real backend (review). The flags are read through getattr(settings, ...) at
# call time, so an override here is enough.
@override_settings(
    TEMPORAL_ENABLED=False,
    TEMPORAL_APPEAL_JOURNEY_ENABLED=False,
    TEMPORAL_INTAKE_JOURNEY_ENABLED=False,
)
class AppealReplayCapTest(TestCase):
    """The replay budget is spent on the newest deliverable drafts, and no more."""

    DENIAL_ID = 4101

    def _create_denial(self):
        email = "replay-cap@example.com"
        # gen_attempts=3 skips the research phase, the same way the sibling
        # tests in test_common_view_logic.py do. At 1, generation bumps it to
        # 2 and enters research, and the RAG helper's health check makes a
        # real HTTP request to whatever RAG_SERVICE_URL is in the ambient
        # environment (review). Nothing here is about research.
        denial = Denial.objects.create(
            denial_id=self.DENIAL_ID,
            semi_sekret="sekret",
            hashed_email=Denial.get_hashed_email(email),
            gen_attempts=3,
        )
        return email, denial

    @staticmethod
    async def _collect_contents(data):
        contents = []
        async for chunk in AppealsBackendHelper.generate_appeals(data):
            if not chunk or not chunk.strip():
                continue
            try:
                parsed = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            if "content" in parsed:
                contents.append(parsed["content"])
        return contents

    def _replay_of_eighteen(self, mock_appeal_generator):
        """Eighteen stored drafts, created oldest to newest; the drafts replayed."""
        email, denial = self._create_denial()
        for i in range(18):
            ProposedAppeal.objects.create(
                for_denial=denial,
                appeal_text=(
                    f"Stored draft {i} for this denial, long enough to be a "
                    "deliverable appeal rather than a runt row."
                ),
                speculative=False,
            )
        mock_appeal_generator.make_appeals.return_value = iter([])

        async def run():
            try:
                contents = await self._collect_contents(
                    {
                        "denial_id": self.DENIAL_ID,
                        "email": email,
                        "semi_sekret": denial.semi_sekret,
                    }
                )
                return [c for c in contents if "Stored draft" in c]
            finally:
                await Denial.objects.filter(denial_id=self.DENIAL_ID).adelete()

        return async_to_sync(run)()

    @pytest.mark.django_db
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_replay_is_capped(self, mock_appeal_generator):
        """Eighteen stored drafts must not all come back."""
        replayed = self._replay_of_eighteen(mock_appeal_generator)
        self.assertEqual(
            len(replayed),
            AppealsBackendHelper.MAX_REPLAYED_APPEALS,
            "the stored-draft replay is uncapped again; the page opens "
            f"with a wall of letters. Got {len(replayed)}",
        )

    @pytest.mark.django_db
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_replay_is_newest_first(self, mock_appeal_generator):
        """The budget goes to the most recent drafts: 17, 16, 15, never 0."""
        replayed = self._replay_of_eighteen(mock_appeal_generator)
        self.assertIn("Stored draft 17", replayed[0])
        self.assertNotIn(
            "Stored draft 0",
            " ".join(replayed),
            "the oldest draft was replayed, so the ordering is not newest-first",
        )

    @pytest.mark.django_db
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_a_denial_under_the_cap_is_unaffected(self, mock_appeal_generator):
        """The common case, a first-time user, must not lose anything."""
        email, denial = self._create_denial()
        for i in range(2):
            ProposedAppeal.objects.create(
                for_denial=denial,
                appeal_text=(
                    f"Only draft {i} for this denial, long enough to count as "
                    "a deliverable appeal rather than a runt row."
                ),
                speculative=False,
            )
        mock_appeal_generator.make_appeals.return_value = iter([])

        async def test():
            try:
                contents = await self._collect_contents(
                    {
                        "denial_id": self.DENIAL_ID,
                        "email": email,
                        "semi_sekret": denial.semi_sekret,
                    }
                )
                replayed = [c for c in contents if "Only draft" in c]
                self.assertEqual(len(replayed), 2)
            finally:
                await Denial.objects.filter(denial_id=self.DENIAL_ID).adelete()

        async_to_sync(test)()

    @pytest.mark.django_db
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_the_cap_counts_delivered_drafts_not_rows_examined(
        self, mock_appeal_generator
    ):
        """Duplicates and runts are skipped and must not spend the budget.

        Otherwise a denial whose newest rows happen to be twins would replay
        almost nothing, which is the same bug in the other direction.
        """
        email, denial = self._create_denial()
        # Oldest: three distinct, deliverable drafts.
        for i in range(3):
            ProposedAppeal.objects.create(
                for_denial=denial,
                appeal_text=(
                    f"Distinct draft {i} for this denial, long enough to be a "
                    "deliverable appeal rather than a runt row."
                ),
                speculative=False,
            )
        # Newest: a runt, then two rows holding the same normalized text.
        ProposedAppeal.objects.create(
            for_denial=denial, appeal_text="tiny", speculative=False
        )
        dup_text = (
            "A duplicated newest draft, long enough to be deliverable but "
            "present twice over."
        )
        # The partial unique constraint on (for_denial, text_fingerprint) makes
        # this shape impossible to CREATE today, which is the point: it only
        # exists as legacy rows written before the fingerprint field, where the
        # fingerprint is NULL and the constraint does not apply. Null them out
        # to reproduce that, rather than testing a state the DB now prevents.
        for i in range(2):
            row = ProposedAppeal.objects.create(
                for_denial=denial,
                appeal_text=f"{dup_text} {i}",
                speculative=False,
            )
            ProposedAppeal.objects.filter(pk=row.pk).update(
                appeal_text=dup_text, text_fingerprint=None
            )
        mock_appeal_generator.make_appeals.return_value = iter([])

        async def test():
            try:
                contents = await self._collect_contents(
                    {
                        "denial_id": self.DENIAL_ID,
                        "email": email,
                        "semi_sekret": denial.semi_sekret,
                    }
                )
                replayed = [
                    c
                    for c in contents
                    if "Distinct draft" in c or "duplicated newest draft" in c
                ]
                self.assertEqual(
                    len(replayed),
                    AppealsBackendHelper.MAX_REPLAYED_APPEALS,
                    "skipped rows spent the replay budget, so the user got "
                    f"fewer drafts than the cap allows. Got {len(replayed)}",
                )
                self.assertEqual(
                    len([c for c in replayed if "duplicated newest draft" in c]),
                    1,
                    "the duplicate was delivered twice",
                )
            finally:
                await Denial.objects.filter(denial_id=self.DENIAL_ID).adelete()

        async_to_sync(test)()

    @pytest.mark.django_db
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_a_synthesis_result_matching_a_held_back_draft_is_still_delivered(
        self, mock_appeal_generator
    ):
        """The cap hides a draft; it must not make the synthesis of it vanish.

        Synthesis inputs are marked served so the end-of-flow reconciliation
        does not dump them. Marking a HELD-BACK input served made the
        verbatim-copy guard discard a synthesis result that matched it, so the
        user never saw the one copy they could have (review). Held-back rows
        now stay out of served_keys; the result lands as the stored row.
        """
        email, denial = self._create_denial()
        texts = [
            (
                f"Stored draft {i} for this denial, long enough to be a "
                "deliverable appeal rather than a runt row."
            )
            for i in range(18)
        ]
        for text in texts:
            ProposedAppeal.objects.create(
                for_denial=denial, appeal_text=text, speculative=False
            )
        mock_appeal_generator.make_appeals.return_value = iter([])
        # Draft 5 is well past the cap of three (17, 16, 15 are replayed).
        mock_appeal_generator.synthesize_appeals = AsyncMock(return_value=texts[5])

        async def test():
            try:
                contents = await self._collect_contents(
                    {
                        "denial_id": self.DENIAL_ID,
                        "email": email,
                        "semi_sekret": denial.semi_sekret,
                    }
                )
                stored = [c for c in contents if "Stored draft" in c]
                fives = [c for c in stored if "Stored draft 5 " in c]
                self.assertEqual(
                    len(fives),
                    1,
                    "the synthesis result that matched a held-back draft was "
                    f"discarded (or duplicated): {len(fives)} copies",
                )
                # Three replayed plus the one synthesis landed on, and nothing
                # else: the held-back rows must not come back at the end.
                self.assertEqual(
                    len(stored),
                    AppealsBackendHelper.MAX_REPLAYED_APPEALS + 1,
                    f"unexpected stored drafts on screen: {len(stored)}",
                )
            finally:
                await Denial.objects.filter(denial_id=self.DENIAL_ID).adelete()

        async_to_sync(test)()
