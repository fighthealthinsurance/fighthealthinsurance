"""Stored drafts are replayed newest-first and capped, not dumped in full.

Rows accumulate per denial across retries, reconnects and re-runs of the same
denial. The replay had no cap, so a denial that had been generated against a few
times opened with a wall of stored letters and buried the fresh draft at the
bottom. Melanie hit exactly this on prod: eighteen stored drafts, then one new
one, last.

These pin the cap and the ordering. They are deliberately independent of
TypeSafe scoring: with the ranking flags off there are no scores to sort by, and
the page still must not open with a haystack.
"""

import json
from unittest.mock import patch

import pytest
from asgiref.sync import async_to_sync
from django.test import TestCase

from fighthealthinsurance.common_view_logic import AppealsBackendHelper
from fighthealthinsurance.models import Denial, ProposedAppeal


class AppealReplayCapTest(TestCase):
    """The replay budget is spent on the newest deliverable drafts, and no more."""

    DENIAL_ID = 4101

    def _create_denial(self):
        email = "replay-cap@example.com"
        denial = Denial.objects.create(
            denial_id=self.DENIAL_ID,
            semi_sekret="sekret",
            hashed_email=Denial.get_hashed_email(email),
            gen_attempts=1,
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

    @pytest.mark.django_db
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_replay_is_capped_and_newest_first(self, mock_appeal_generator):
        """Eighteen stored drafts must not all come back."""
        email, denial = self._create_denial()
        # Created oldest to newest, so "Stored draft 17" is the most recent.
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

        async def test():
            try:
                contents = await self._collect_contents(
                    {
                        "denial_id": self.DENIAL_ID,
                        "email": email,
                        "semi_sekret": denial.semi_sekret,
                    }
                )
                replayed = [c for c in contents if "Stored draft" in c]
                self.assertEqual(
                    len(replayed),
                    AppealsBackendHelper.MAX_REPLAYED_APPEALS,
                    "the stored-draft replay is uncapped again; the page opens "
                    f"with a wall of letters. Got {len(replayed)}",
                )
                # Newest first: 17, 16, 15 -- never 0, 1, 2.
                self.assertIn("Stored draft 17", replayed[0])
                self.assertNotIn(
                    "Stored draft 0",
                    " ".join(replayed),
                    "the oldest draft was replayed, so the ordering is not "
                    "newest-first",
                )
            finally:
                await Denial.objects.filter(denial_id=self.DENIAL_ID).adelete()

        async_to_sync(test)()

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
