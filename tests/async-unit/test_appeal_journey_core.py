"""Integration tests for appeal_journey_core against the REAL generator.

These exercise the database-backed core consuming the actual
``AppealsBackendHelper.generate_appeals`` iterator with only the model layer
(``appealGenerator``) stubbed -- the layer where the PR #963 review found the
failures: substituted re-served drafts fooling text-based progress counting,
empty streams reported as success, and speculative reserves counted as
delivered drafts. Denials are created with ``gen_attempts=3`` so the research
phase is skipped and the tests stay fast.

KNOWN LOCAL-RIG QUIRK (Postgres-in-Docker on macOS): running the full
TestGenerateAndStoreAppeals class in one process can fail the retry test
with one draft short; every test passes in isolation and in any pair, so
the interaction is cumulative cross-test executor/connection state, not a
defect in the code under test. Linux CI runs the full suite as the gate.
"""

from unittest.mock import AsyncMock, patch

import pytest
from django.test import TransactionTestCase

from fighthealthinsurance import appeal_journey_core
from fighthealthinsurance.models import Denial, ProposedAppeal
from fighthealthinsurance.generate_appeal import GeneratedAppeal


def _drafts(texts):
    """Wrap plain strings as the GeneratedAppeal drafts make_appeals emits."""
    return [
        GeneratedAppeal(text=t, model_name="fhi-internal", context_level="full")
        for t in texts
    ]


def _make_denial(denial_id, gen_attempts=3):
    email = f"journey_core_{denial_id}@example.com"
    return Denial.objects.create(
        denial_id=denial_id,
        denial_text="Coverage for the requested MRI was denied as not medically necessary.",
        semi_sekret="sekret",
        hashed_email=Denial.get_hashed_email(email),
        gen_attempts=gen_attempts,
    )


class _JourneyTestBase(TransactionTestCase):
    """Shared setup: stub the generator's fire-and-forget context warmers
    (RAG, ML citations, payer policy). Left real, their background tasks
    outlive one test and interfere with the next test's saves under
    TransactionTestCase -- and they have no bearing on journey semantics."""

    def setUp(self):
        super().setUp()
        for target in (
            "fighthealthinsurance.common_view_logic.get_rag_context_for_denial",
            "fighthealthinsurance.common_view_logic.MLCitationsHelper.generate_citations_for_denial",
        ):
            patcher = patch(target, new_callable=AsyncMock, return_value=None)
            patcher.start()
            self.addCleanup(patcher.stop)
        pmt_patcher = patch(
            "fighthealthinsurance.common_view_logic.AppealsBackendHelper.pmt"
        )
        pmt = pmt_patcher.start()
        pmt.find_context_for_denial = AsyncMock(return_value=None)
        self.addCleanup(pmt_patcher.stop)


# TransactionTestCase, not TestCase: the generator's connection hygiene
# closes connections mid-flow, which kills TestCase's single wrapped
# test transaction; without the wrapper, closed connections just reopen
# (matching production behavior).
class TestGenerateAndStoreAppeals(_JourneyTestBase):
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_substituted_existing_drafts_cannot_satisfy_the_target(self, mock_gen):
        """Two existing placeholder drafts + one generated draft = exactly one
        new durable row. The generator re-serves existing drafts transformed
        by sub_in_appeals, so text-based progress counting saw them as new and
        stopped before generating; durable-ID counting must not."""
        denial = _make_denial(9101)
        for i in range(2):
            ProposedAppeal.objects.create(
                for_denial=denial,
                appeal_text=f"Dear [Insurance Company],\n\nExisting draft {i} for [Patient Name].",
            )
        mock_gen.make_appeals.return_value = iter(
            _drafts(["Dear Reviewer, a genuinely new appeal citing the denial."])
        )

        stored = appeal_journey_core.generate_and_store_appeals(denial)

        assert stored == 1
        assert (
            ProposedAppeal.objects.filter(for_denial=denial, speculative=False).count()
            == 3
        )

    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_empty_stream_raises_journey_incomplete(self, mock_gen):
        """A generator that produces nothing must surface as a retryable
        failure, never as a successful activity with zero drafts."""
        denial = _make_denial(9102)
        mock_gen.make_appeals.return_value = iter([])

        with pytest.raises(appeal_journey_core.JourneyIncomplete):
            appeal_journey_core.generate_and_store_appeals(denial)

    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_retry_after_partial_attempt_tops_up_to_target(self, mock_gen):
        """First attempt persists one draft and fails the postcondition; the
        retry generates the remainder and reaches exactly the target."""
        denial = _make_denial(9103)
        mock_gen.make_appeals.return_value = iter(
            _drafts(
                [
                    "Dear Reviewer, this appeal contests the denial on medical "
                    "necessity grounds: my physician documented the failure of "
                    "conservative treatment and the need for advanced imaging."
                ]
            )
        )
        with pytest.raises(appeal_journey_core.JourneyIncomplete):
            appeal_journey_core.generate_and_store_appeals(denial)
        assert ProposedAppeal.objects.filter(for_denial=denial).count() == 1

        # Two genuinely DIFFERENT letters: near-identical texts would be
        # (correctly) suppressed by the near-duplicate check and never count.
        mock_gen.make_appeals.return_value = iter(
            _drafts(
                [
                    "To the appeals board: the plan's own coverage policy "
                    "states imaging is covered after failed conservative care, "
                    "which my records demonstrate across six documented visits.",
                    "I am requesting an independent review because the denial "
                    "letter mischaracterizes my treatment history and ignores "
                    "the specialist's written recommendation for this study.",
                ]
            )
        )
        stored = appeal_journey_core.generate_and_store_appeals(denial)
        assert stored == 2
        assert (
            ProposedAppeal.objects.filter(for_denial=denial, speculative=False).count()
            == appeal_journey_core.TARGET_APPEALS
        )

    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_speculative_reserves_do_not_satisfy_the_target(self, mock_gen):
        """Reserve precompute rows are not delivered drafts: with three
        speculative rows present the journey still generates."""
        denial = _make_denial(9104)
        for i in range(3):
            ProposedAppeal.objects.create(
                for_denial=denial,
                appeal_text=f"Reserve draft {i}.",
                speculative=True,
            )
        assert (
            appeal_journey_core.precheck_appeal_journey(denial)
            == appeal_journey_core.STATUS_OK
        )
        mock_gen.make_appeals.return_value = iter(
            _drafts(
                [
                    # Long enough to clear the generator's runt filter.
                    f"Dear Reviewer, appeal draft {i}: my physician documented "
                    "months of conservative treatment without improvement and "
                    "the requested imaging is medically necessary to plan care."
                    for i in ("one", "two", "three")
                ]
            )
        )
        stored = appeal_journey_core.generate_and_store_appeals(denial)
        assert stored == 3


class TestLoadDenial(TransactionTestCase):
    def test_malformed_uuid_is_terminal_not_retry_fuel(self):
        """An invalid uuid must return None (terminal not_found), not raise a
        ValidationError into the precheck's unlimited retry."""
        assert appeal_journey_core.load_denial("h", "not-a-uuid") is None


class TestCandidateCounting(_JourneyTestBase):
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_chosen_row_means_journey_complete(self, mock_gen):
        """A chosen row is the user's pick, not a draft: precheck must be
        terminal even with fewer than three candidate rows."""
        denial = _make_denial(9106)
        ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text="The letter the user picked and finished with.",
            chosen=True,
        )
        assert (
            appeal_journey_core.precheck_appeal_journey(denial)
            == appeal_journey_core.STATUS_ALREADY_HAS_APPEALS
        )

    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_runt_rows_do_not_satisfy_the_target(self, mock_gen):
        """Legacy empty/runt rows are not deliverable drafts; three of them
        must not convince precheck the journey is done."""
        denial = _make_denial(9107)
        for i in range(3):
            ProposedAppeal.objects.create(for_denial=denial, appeal_text=f"x{i}")
        assert (
            appeal_journey_core.precheck_appeal_journey(denial)
            == appeal_journey_core.STATUS_OK
        )


class TestFingerprintCompleteness(_JourneyTestBase):
    """The distinct-fingerprint counting rules from the external review:
    duplicate rows are one draft, and every write path fingerprints."""

    def test_unchosen_rows_fingerprint_themselves_on_save(self):
        denial = _make_denial(9108)
        row = ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text="Dear Reviewer, a real draft with enough words to count.",
        )
        assert row.text_fingerprint == ProposedAppeal.fingerprint(row.appeal_text)

    def test_chosen_rows_carry_no_fingerprint(self):
        """A chosen row is a COPY of the picked draft; a fingerprint there
        would collide with the original draft's row."""
        denial = _make_denial(9109)
        row = ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text="The letter the user picked.",
            chosen=True,
        )
        assert row.text_fingerprint is None

    def test_legacy_duplicate_rows_do_not_satisfy_the_precheck(self):
        """Three NULL-fingerprint copies of one letter (the pre-constraint
        double-store shape; bulk_create bypasses save() exactly like the old
        writers bypassed fingerprinting) are not three drafts."""
        denial = _make_denial(9110)
        letter = (
            "Dear Reviewer, this appeal contests the denial because my "
            "physician documented medical necessity across repeated visits."
        )
        ProposedAppeal.objects.bulk_create(
            ProposedAppeal(for_denial=denial, appeal_text=letter) for _ in range(3)
        )
        assert (
            appeal_journey_core.precheck_appeal_journey(denial)
            == appeal_journey_core.STATUS_OK
        )

    def test_three_distinct_drafts_satisfy_the_precheck(self):
        denial = _make_denial(9111)
        for i in ("first", "second", "third"):
            ProposedAppeal.objects.create(
                for_denial=denial,
                appeal_text=(
                    f"Dear Reviewer, the {i} distinct appeal citing the plan's "
                    "own coverage policy and my documented treatment history."
                ),
            )
        assert (
            appeal_journey_core.precheck_appeal_journey(denial)
            == appeal_journey_core.STATUS_ALREADY_HAS_APPEALS
        )

    def test_duplicate_content_cannot_be_stored_twice(self):
        """With save() fingerprinting every un-chosen row, the partial unique
        constraint now binds ALL writers, not just save_appeal."""
        import pytest as _pytest
        from django.db import IntegrityError

        denial = _make_denial(9112)
        text = "Dear Reviewer, the same letter twice must be one row."
        ProposedAppeal.objects.create(for_denial=denial, appeal_text=text)
        with _pytest.raises(IntegrityError):
            ProposedAppeal.objects.create(
                for_denial=denial, appeal_text="  dear   reviewer, THE same "
                "letter twice must be one row."
            )

    def test_backfill_fingerprints_skips_duplicates_and_fills_the_rest(self):
        """The 0202 data migration: legacy NULL rows get fingerprints; a
        duplicate of an already-claimed fingerprint stays NULL (the
        known-legacy-duplicate marker the counting rules exclude)."""
        import importlib

        backfill = importlib.import_module(
            "fighthealthinsurance.migrations.0202_backfill_proposedappeal_fingerprints"
        ).backfill_fingerprints
        from django.apps import apps

        denial = _make_denial(9113)
        letter = "Dear Reviewer, one letter stored twice in the legacy era."
        other = "Dear Reviewer, a different letter from the same era."
        ProposedAppeal.objects.bulk_create(
            [
                ProposedAppeal(for_denial=denial, appeal_text=letter),
                ProposedAppeal(for_denial=denial, appeal_text=letter),
                ProposedAppeal(for_denial=denial, appeal_text=other),
            ]
        )
        backfill(apps, None)
        fps = list(
            ProposedAppeal.objects.filter(for_denial=denial).values_list(
                "text_fingerprint", flat=True
            )
        )
        assert fps.count(None) == 1  # the duplicate copy stays NULL
        assert {f for f in fps if f is not None} == {
            ProposedAppeal.fingerprint(letter),
            ProposedAppeal.fingerprint(other),
        }

    def test_editing_an_unchosen_row_rekeys_its_fingerprint(self):
        """A stale fingerprint would let the edited content be stored again
        as a 'different' draft and block re-storing the original (review)."""
        denial = _make_denial(9114)
        row = ProposedAppeal.objects.create(
            for_denial=denial, appeal_text="Dear Reviewer, the first version."
        )
        row.appeal_text = "Dear Reviewer, the edited version."
        row.save()
        row.refresh_from_db()
        assert row.text_fingerprint == ProposedAppeal.fingerprint(
            "Dear Reviewer, the edited version."
        )

    def test_partial_save_persists_the_rekey(self):
        denial = _make_denial(9115)
        row = ProposedAppeal.objects.create(
            for_denial=denial, appeal_text="Dear Reviewer, before the edit."
        )
        row.appeal_text = "Dear Reviewer, after the edit."
        row.save(update_fields=["appeal_text"])
        row.refresh_from_db()
        assert row.text_fingerprint == ProposedAppeal.fingerprint(
            "Dear Reviewer, after the edit."
        )

    def test_legacy_null_row_survives_unrelated_saves(self):
        """The backfill leaves duplicate rows NULL; a later save of some
        other field must not recompute the fingerprint and trip the
        constraint against the row's fingerprinted twin."""
        denial = _make_denial(9116)
        letter = "Dear Reviewer, the twice-stored legacy letter."
        keeper = ProposedAppeal.objects.create(for_denial=denial, appeal_text=letter)
        (dup,) = ProposedAppeal.objects.bulk_create(
            [ProposedAppeal(for_denial=denial, appeal_text=letter)]
        )
        dup.refresh_from_db()
        assert dup.text_fingerprint is None
        dup.model_name = "fhi-internal"
        dup.save()
        dup.refresh_from_db()
        assert dup.text_fingerprint is None
        assert keeper.pk != dup.pk

    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_live_draft_matching_unserved_reserve_promotes_the_reserve(
        self, mock_gen
    ):
        """A fast live generation can produce the same letter a speculative
        reserve already holds. The insert conflicts on the fingerprint; the
        reuse path must atomically PROMOTE the reserve row, or the streamed
        draft's row stays speculative=True and the appeal the user just
        watched disappears from every later read (external review)."""
        denial = _make_denial(9117)
        letter = (
            "Dear Reviewer, the reserve and the live run agree on this "
            "letter about documented medical necessity."
        )
        reserve = ProposedAppeal.objects.create(
            for_denial=denial, appeal_text=letter, speculative=True
        )
        mock_gen.make_appeals.return_value = iter(_drafts([letter]))
        with pytest.raises(appeal_journey_core.JourneyIncomplete):
            # 1 durable draft < TARGET, so the postcondition still raises --
            # the assertions below are the point.
            appeal_journey_core.generate_and_store_appeals(denial)
        reserve.refresh_from_db()
        assert reserve.speculative is False
        assert ProposedAppeal.objects.filter(for_denial=denial).count() == 1

    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_case_variant_of_reserve_collides_and_promotes_not_duplicates(
        self, mock_gen
    ):
        """Fingerprints are case/whitespace-normalized, so a trivial variant
        of a reserve letter must also land on the reserve row (promoted),
        never as a second near-identical draft."""
        denial = _make_denial(9118)
        letter = (
            "Dear Reviewer, my physician documented repeated conservative "
            "care before requesting this imaging study."
        )
        ProposedAppeal.objects.create(
            for_denial=denial, appeal_text=letter, speculative=True
        )
        mock_gen.make_appeals.return_value = iter(_drafts([letter.upper()]))
        with pytest.raises(appeal_journey_core.JourneyIncomplete):
            appeal_journey_core.generate_and_store_appeals(denial)
        rows = list(ProposedAppeal.objects.filter(for_denial=denial))
        assert len(rows) == 1
        assert rows[0].speculative is False

    def test_legacy_null_row_edited_to_unique_text_rekeys(self):
        """A legacy duplicate edited to genuinely new content must rejoin
        the constraint and journey counting -- NULL is the marker for
        known duplicates, not a permanent exemption (review)."""
        denial = _make_denial(9119)
        letter = "Dear Reviewer, the legacy letter stored twice back then."
        ProposedAppeal.objects.create(for_denial=denial, appeal_text=letter)
        (dup,) = ProposedAppeal.objects.bulk_create(
            [ProposedAppeal(for_denial=denial, appeal_text=letter)]
        )
        dup = ProposedAppeal.objects.get(pk=dup.pk)
        assert dup.text_fingerprint is None
        dup.appeal_text = "Dear Reviewer, entirely new content after an edit."
        dup.save(update_fields=["appeal_text"])
        dup.refresh_from_db()
        assert dup.text_fingerprint == ProposedAppeal.fingerprint(dup.appeal_text)

    def test_legacy_null_row_full_save_with_unchanged_text_stays_null(self):
        denial = _make_denial(9120)
        letter = "Dear Reviewer, one more twice-stored legacy letter."
        ProposedAppeal.objects.create(for_denial=denial, appeal_text=letter)
        (dup,) = ProposedAppeal.objects.bulk_create(
            [ProposedAppeal(for_denial=denial, appeal_text=letter)]
        )
        dup = ProposedAppeal.objects.get(pk=dup.pk)
        dup.model_name = "fhi-internal"
        dup.save()  # full save, text unchanged: must not recompute/collide
        dup.refresh_from_db()
        assert dup.text_fingerprint is None
