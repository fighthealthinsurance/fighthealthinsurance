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

    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_variant_of_existing_draft_streams_once_against_one_row(self, mock_gen):
        """Client-visible corruption from external review: an existing draft
        is re-served, then the model emits a case/whitespace VARIANT. Exact-
        string dedupe let it through, the insert collided on the normalized
        fingerprint, and the variant streamed under the stored row's id --
        two drafts on screen, one row in the database, one 'lost' on reload.
        Frames, ids and rows must all agree."""
        import json as _json

        from asgiref.sync import async_to_sync

        from fighthealthinsurance.common_view_logic import AppealsBackendHelper

        denial = _make_denial(9121)
        letter = (
            "Dear Reviewer, this appeal contests the denial because my "
            "physician documented medical necessity across repeated visits."
        )
        ProposedAppeal.objects.create(for_denial=denial, appeal_text=letter)
        mock_gen.make_appeals.return_value = iter(_drafts([letter.upper()]))

        async def collect():
            frames = []
            async for chunk in AppealsBackendHelper.generate_appeals_for_denial(
                denial
            ):
                if appeal_journey_core._appeal_text_from_chunk(chunk) is None:
                    continue
                data = _json.loads(chunk)
                if "id" in data:
                    frames.append(data)
            return frames

        frames = async_to_sync(collect)()
        assert len(frames) == 1, [f.get("id") for f in frames]
        assert len({f["id"] for f in frames}) == 1
        assert ProposedAppeal.objects.filter(for_denial=denial).count() == 1

    def test_backfill_never_attaches_a_stale_fingerprint_to_edited_text(self):
        """A concurrent edit between the backfill's read and its write must
        not leave fp(A) on text B (external review). The write is guarded by
        the observed text; a miss re-reads and recomputes."""
        from unittest.mock import patch as _patch

        from fighthealthinsurance import appeal_fingerprints

        denial = _make_denial(9122)
        text_a = "Dear Reviewer, the original legacy text before an edit."
        text_b = "Dear Reviewer, the edited text that landed mid-backfill."
        (row,) = ProposedAppeal.objects.bulk_create(
            [ProposedAppeal(for_denial=denial, appeal_text=text_a)]
        )
        real = appeal_fingerprints.fingerprint_text
        calls = {"n": 0}

        def edit_between_read_and_write(text):
            calls["n"] += 1
            if calls["n"] == 1:
                # The "old worker" edits the row after the backfill read it.
                ProposedAppeal.objects.filter(pk=row.pk).update(appeal_text=text_b)
            return real(text)

        with _patch.object(
            appeal_fingerprints, "fingerprint_text", edit_between_read_and_write
        ):
            outcome = appeal_fingerprints.fill_row(ProposedAppeal, row.pk)
        row.refresh_from_db()
        assert outcome == appeal_fingerprints.FILLED
        assert row.text_fingerprint == ProposedAppeal.fingerprint(text_b)

    def test_strict_backfill_fails_while_writers_are_active(self):
        from unittest.mock import patch as _patch

        from django.core.management import call_command
        from django.core.management.base import CommandError

        quiet = {
            "filled": 0,
            "skipped_duplicate": 0,
            "skipped_empty": 0,
            "lost_race": 0,
            "remaining_null": 0,
        }
        busy = dict(quiet, filled=2)
        target = "fighthealthinsurance.management.commands.backfill_appeal_fingerprints.run_backfill"
        with _patch(target, side_effect=[busy, busy]):
            with pytest.raises(CommandError):
                call_command("backfill_appeal_fingerprints", "--strict")
        with _patch(target, side_effect=[busy, quiet]):
            call_command("backfill_appeal_fingerprints", "--strict")

    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_legacy_duplicate_existing_rows_stream_once(self, mock_gen):
        """Two legacy NULL-fingerprint twins are one draft: the existing-
        rows loop streams the first and skips the second (review)."""
        import json as _json

        from asgiref.sync import async_to_sync

        from fighthealthinsurance.common_view_logic import AppealsBackendHelper

        denial = _make_denial(9123)
        letter = (
            "Dear Reviewer, this legacy letter was stored twice before the "
            "fingerprint constraint existed and must stream only once."
        )
        ProposedAppeal.objects.bulk_create(
            [ProposedAppeal(for_denial=denial, appeal_text=letter) for _ in range(2)]
        )
        mock_gen.make_appeals.return_value = iter([])

        async def collect():
            frames = []
            async for chunk in AppealsBackendHelper.generate_appeals_for_denial(
                denial
            ):
                if appeal_journey_core._appeal_text_from_chunk(chunk) is None:
                    continue
                data = _json.loads(chunk)
                if "id" in data:
                    frames.append(data)
            return frames

        assert len(async_to_sync(collect)()) == 1

    def test_partial_save_excluding_text_leaves_fingerprint_alone(self):
        """In-memory text change + save(update_fields=['model_name']) must
        not persist a fingerprint for text the row does not hold (review)."""
        denial = _make_denial(9124)
        row = ProposedAppeal.objects.create(
            for_denial=denial, appeal_text="Dear Reviewer, the stored text."
        )
        original_fp = row.text_fingerprint
        row.appeal_text = "Dear Reviewer, an unsaved in-memory edit."
        row.model_name = "fhi-internal"
        row.save(update_fields=["model_name"])
        row.refresh_from_db()
        assert row.appeal_text == "Dear Reviewer, the stored text."
        assert row.text_fingerprint == original_fp

    def test_strict_backfill_fails_on_unclassified_null_rows(self):
        from unittest.mock import patch as _patch

        from django.core.management import call_command
        from django.core.management.base import CommandError

        quiet = {
            "filled": 0,
            "skipped_duplicate": 1,
            "skipped_empty": 0,
            "lost_race": 0,
            "remaining_null": 1,
        }
        sneaky = dict(quiet, remaining_null=2)  # a NULL row the pass never saw
        target = "fighthealthinsurance.management.commands.backfill_appeal_fingerprints.run_backfill"
        with _patch(target, side_effect=[quiet, sneaky]):
            with pytest.raises(CommandError):
                call_command("backfill_appeal_fingerprints", "--strict")
        with _patch(target, side_effect=[quiet, quiet]):
            call_command("backfill_appeal_fingerprints", "--strict")

    def test_verify_rekeys_a_fingerprint_that_no_longer_matches_its_text(self):
        """A pod on pre-fingerprint code can EDIT text under a stale
        fingerprint (its save() never re-keys). NULL checks cannot see that;
        the integrity pass must (review)."""
        from fighthealthinsurance import appeal_fingerprints

        denial = _make_denial(9125)
        row = ProposedAppeal.objects.create(
            for_denial=denial, appeal_text="Dear Reviewer, the text as first saved."
        )
        stale_fp = row.text_fingerprint
        # Old-style edit: text changes, fingerprint left behind.
        ProposedAppeal.objects.filter(pk=row.pk).update(
            appeal_text="Dear Reviewer, the text after an old-pod edit."
        )
        counts = appeal_fingerprints.verify_fingerprints(ProposedAppeal)
        row.refresh_from_db()
        assert counts[appeal_fingerprints.REKEYED] == 1
        assert row.text_fingerprint != stale_fp
        assert row.text_fingerprint == ProposedAppeal.fingerprint(row.appeal_text)

    def test_verify_clears_a_mismatch_that_would_collide_to_null(self):
        """An old-pod edit that turns a row into a twin of another row: the
        stale fingerprint must not be LEFT in place (it would count the twin's
        content twice). It is cleared to NULL -- the known-duplicate marker --
        and counted as a repair, so strict mode retries (review)."""
        from django.core.management import call_command
        from django.core.management.base import CommandError

        from fighthealthinsurance import appeal_fingerprints

        denial = _make_denial(9126)
        keeper_text = "Dear Reviewer, the letter that already owns its key."
        ProposedAppeal.objects.create(for_denial=denial, appeal_text=keeper_text)
        other = ProposedAppeal.objects.create(
            for_denial=denial, appeal_text="Dear Reviewer, a different letter."
        )
        # Old-style edit that makes `other` a twin of the keeper.
        ProposedAppeal.objects.filter(pk=other.pk).update(appeal_text=keeper_text)
        # Strict mode: fill passes are quiet, verify repairs one row -> fail.
        with pytest.raises(CommandError):
            call_command("backfill_appeal_fingerprints", "--strict")
        other.refresh_from_db()
        assert other.text_fingerprint is None
        # Retry (what the Job's backoff does): now quiescent.
        call_command("backfill_appeal_fingerprints", "--strict")
        counts = appeal_fingerprints.verify_fingerprints(ProposedAppeal)
        assert counts[appeal_fingerprints.REKEYED] == 0

    def test_verify_clears_the_fingerprint_of_text_edited_to_blank(self):
        from fighthealthinsurance import appeal_fingerprints

        denial = _make_denial(9127)
        row = ProposedAppeal.objects.create(
            for_denial=denial, appeal_text="Dear Reviewer, soon to be blanked."
        )
        ProposedAppeal.objects.filter(pk=row.pk).update(appeal_text="   ")
        counts = appeal_fingerprints.verify_fingerprints(ProposedAppeal)
        row.refresh_from_db()
        assert counts[appeal_fingerprints.REKEYED] == 1
        assert row.text_fingerprint is None

    def test_strict_backfill_fails_when_verify_had_to_rekey(self):
        from unittest.mock import patch as _patch

        from django.core.management import call_command
        from django.core.management.base import CommandError

        quiet = {
            "filled": 0,
            "skipped_duplicate": 0,
            "skipped_empty": 0,
            "lost_race": 0,
            "remaining_null": 0,
        }
        base = "fighthealthinsurance.management.commands.backfill_appeal_fingerprints."
        with _patch(base + "run_backfill", side_effect=[quiet, quiet]), _patch(
            base + "verify_fingerprints",
            return_value={"rekeyed": 1, "mismatch_duplicate": 0, "checked": 5},
        ):
            with pytest.raises(CommandError):
                call_command("backfill_appeal_fingerprints", "--strict")
        with _patch(base + "run_backfill", side_effect=[quiet, quiet]), _patch(
            base + "verify_fingerprints",
            return_value={"rekeyed": 0, "mismatch_duplicate": 0, "checked": 5},
        ):
            call_command("backfill_appeal_fingerprints", "--strict")

    def test_review_scenario_stale_key_after_old_pod_edit_is_repaired_end_to_end(self):
        """Review 10's exact chain: rows A/B/C each fingerprinted; a pod on
        old code edits C's text to A's, leaving fp(C) in place. Strict must
        fail on the first run (a repair happened), C's fingerprint must be
        NULL afterwards (known duplicate, not a third distinct draft), the
        retry must pass, and the journey precheck must see TWO drafts."""
        from django.core.management import call_command
        from django.core.management.base import CommandError

        denial = _make_denial(9128)
        text_a = "Dear Reviewer, letter A about documented medical necessity."
        text_b = "Dear Reviewer, letter B about the plan's own coverage policy."
        text_c = "Dear Reviewer, letter C requesting an independent review."
        row_a = ProposedAppeal.objects.create(for_denial=denial, appeal_text=text_a)
        ProposedAppeal.objects.create(for_denial=denial, appeal_text=text_b)
        row_c = ProposedAppeal.objects.create(for_denial=denial, appeal_text=text_c)
        fp_c = row_c.text_fingerprint
        assert fp_c is not None
        # Old-pod edit: text changes, fingerprint left behind.
        ProposedAppeal.objects.filter(pk=row_c.pk).update(appeal_text=text_a)

        with pytest.raises(CommandError):
            call_command("backfill_appeal_fingerprints", "--strict")
        row_c.refresh_from_db()
        assert row_c.text_fingerprint is None
        row_a.refresh_from_db()
        assert row_a.text_fingerprint == ProposedAppeal.fingerprint(text_a)

        call_command("backfill_appeal_fingerprints", "--strict")  # retry: quiet

        distinct = set(
            ProposedAppeal.objects.filter(
                for_denial=denial, chosen=False, text_fingerprint__isnull=False
            ).values_list("text_fingerprint", flat=True)
        )
        assert len(distinct) == 2
        assert (
            appeal_journey_core.precheck_appeal_journey(denial)
            == appeal_journey_core.STATUS_OK  # 2 < TARGET_APPEALS: not "done"
        )


def _build_script() -> str:
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    return (root / "scripts" / "build.sh").read_text()


def test_deploy_script_waits_for_the_strict_backfill_job():
    """build.sh must not just apply the strict backfill Job -- it must wait
    for it to complete and fail the deploy otherwise (review 10)."""
    build = _build_script()
    apply_at = build.index("backfill-fingerprints-job.yaml | kubectl apply -f -")
    wait_at = build.index("backfill_done=true")
    assert wait_at > apply_at
    tail = build[wait_at:]
    assert 'if [ "$backfill_done" != true ]' in tail
    assert "exit 1" in tail
    for dep in ("web", "fhi-fax-worker", "fhi-appeal-worker"):
        assert "rollout status deployment" in build and dep in build


def test_deploy_script_fails_fast_on_a_failed_backfill_job():
    """`kubectl wait --for=condition=complete` ignores condition=Failed, so a
    Job that gives up would burn the full 30m before the deploy noticed. The
    poll has to look at both conditions (external review)."""
    build = _build_script()
    assert "Complete=True" in build
    failed_at = build.index('*"Failed=True"*')
    # ...and act on it: the Failed branch exits rather than continuing to poll.
    assert "exit 1" in build[failed_at : failed_at + 500]
    # ...and the blind wait is gone from the executed script (the comment
    # explaining why it is gone naturally still names it).
    code = [ln for ln in build.splitlines() if not ln.lstrip().startswith("#")]
    assert not any("wait --for=condition=complete" in ln for ln in code)


def test_backfill_poll_treats_an_unreadable_job_status_as_fatal():
    """A failed `kubectl get job` must not read as 'not complete yet' -- that
    turns an API outage into a silent 30m stall and then a bogus timeout
    message (external review)."""
    build = _build_script()
    assert 'if ! conds="$(job_conditions)"; then' in build
    at = build.index('if ! conds="$(job_conditions)"; then')
    assert "exit 1" in build[at : at + 400]


def test_deploy_script_applies_observability_before_any_gate_that_can_exit():
    """A stalled rollout or a failed backfill used to skip the PDB, the
    PodMonitors, the alert rules and the relay CronJob -- shipping a new image
    with no metrics and no alerts. Every one of those applies belongs above
    the first gate that can exit (external review)."""
    build = _build_script()
    gate_at = build.index("# ROLLOUT GATE")
    for manifest in (
        "worker-pdb.yaml",
        "worker-podmonitor.yaml",
        "worker-alerts.yaml",
        "intake-outbox-cronjob.yaml",
        "intake-outbox-alerts.yaml",
        "appeal-worker.yaml",
    ):
        assert build.index(manifest) < gate_at, manifest


def test_deploy_script_guards_the_ray_wait_on_pods_existing():
    """The Ray cluster is deleted and recreated earlier in the same script, so
    `kubectl wait` against a selector the operator has not populated yet fails
    immediately with "no matching resources found" -- killing a deploy that is
    otherwise fine. The Deployments already have an existence guard; Ray needs
    the same one (external review)."""
    build = _build_script()
    wait_at = build.index("ray_expected_pods)")
    guard = build[:wait_at]
    assert "ray_pods_present=false" in guard
    assert 'if [ "$ray_pods_present" = true ]' in guard
    # A cluster with no Ray pods at all is a skip, not a deploy failure.
    assert "skipping the Ray readiness wait" in build


def test_deploy_script_version_is_bumped_past_the_last_deployed_image():
    """build_django.sh short-circuits when the tag already exists in the
    registry, so a deploy on an unbumped FHI_VERSION silently ships the old
    image. v0.23.2a is what prod ran before this tranche."""
    import re

    build = _build_script()
    version = re.search(r"^FHI_VERSION=(\S+)", build, re.MULTILINE).group(1)
    assert version != "v0.23.2a"


def test_deploy_script_rejects_unknown_flags_and_documents_the_real_ones():
    """The flags are the escape hatch from ~90m of waits, so a typo has to be
    a fast, loud failure rather than a silently ignored argument."""
    import pathlib
    import subprocess

    root = pathlib.Path(__file__).resolve().parents[2]
    script = str(root / "scripts" / "build.sh")

    typo = subprocess.run(
        ["bash", script, "--skip-backfil"], capture_output=True, text=True, timeout=60
    )
    assert typo.returncode != 0
    assert "Unknown argument" in typo.stderr

    helped = subprocess.run(
        ["bash", script, "--help"], capture_output=True, text=True, timeout=60
    )
    assert helped.returncode == 0
    for flag in ("--no-build", "--skip-journey-gates", "--skip-backfill"):
        assert flag in helped.stdout


def test_skip_flags_only_skip_waiting_not_the_applies():
    """--skip-journey-gates must still apply the Job (you check it yourself);
    only --skip-backfill opts out of the Job entirely. Neither may sit above
    an apply of the image or the observability manifests."""
    build = _build_script()
    skip_gates_at = build.index('elif [ "$SKIP_JOURNEY_GATES" = true ]')
    end_at = build.index("\nelse\n", skip_gates_at)
    branch = build[skip_gates_at:end_at]
    assert "backfill-fingerprints-job.yaml | kubectl apply -f -" in branch
    assert "rollout status" not in branch


def _extract_shell_function(build: str, name: str) -> str:
    """Pull one `name() { ... }` block out of the deploy script, so it can be
    executed in isolation against a stub kubectl."""
    start = build.index(f"{name}() {{")
    end = build.index("\n}\n", start) + len("\n}\n")
    return build[start:end]


def test_confirmation_prompts_never_fall_through_to_the_next_environment():
    """Both prompts used to print "Invalid response" on anything but y/n and
    then keep going. At the staging prompt that walked a typo straight into
    the production applies (external review)."""
    import re
    import subprocess

    build = _build_script()
    blocks = re.findall(r"case \$yn in.*?esac", build, re.S)
    assert len(blocks) == 2, blocks
    for block in blocks:
        result = subprocess.run(
            ["bash", "-c", "set -e\nyn=maybe\n" + block + '\necho "KEPT_GOING"'],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode != 0, block
        assert "KEPT_GOING" not in result.stdout, block


def test_deployment_presence_check_distinguishes_notfound_from_an_api_error():
    """A command used as an `if` condition does not trip errexit, so the old
    `if kubectl get deployment X >/dev/null 2>&1` read a transient API error
    as "this Deployment does not exist" and silently skipped its gate
    (external review). Only a real NotFound may count as absent."""
    import os
    import pathlib
    import subprocess
    import tempfile

    fn = _extract_shell_function(_build_script(), "deployment_present")

    def run_against(kubectl_body: str):
        with tempfile.TemporaryDirectory() as tmp:
            shim = pathlib.Path(tmp) / "kubectl"
            shim.write_text("#!/bin/bash\n" + kubectl_body + "\n")
            shim.chmod(0o755)
            env = {**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}"}
            script = (
                "set -e\n"
                "KGET=(kubectl -n totallylegitco)\n"
                + fn
                + '\nif deployment_present web; then echo PRESENT; else echo ABSENT; fi\n'
            )
            return subprocess.run(
                ["bash", "-c", script],
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
            )

    found = run_against('echo "deployment.apps/web"; exit 0')
    assert found.returncode == 0 and "PRESENT" in found.stdout

    missing = run_against(
        'echo \'Error from server (NotFound): deployments.apps "web" not found\' >&2; exit 1'
    )
    assert missing.returncode == 0 and "ABSENT" in missing.stdout

    # The one that used to be indistinguishable from "absent".
    broken = run_against(
        "echo 'Error from server (InternalError): an error on the server' >&2; exit 1"
    )
    assert broken.returncode != 0, broken.stdout
    assert "ABSENT" not in broken.stdout
    assert "refusing to guess" in broken.stderr


def test_gate_waits_for_the_writer_pods_to_drain_not_just_to_roll():
    """`rollout status` returns while old pods are still inside their grace
    period, running preStop and finishing in-flight work -- and still able to
    INSERT a ProposedAppeal. The two ProposedAppeal-writing Deployments must
    additionally be drained; the fax worker must not be, since it writes none
    and its 1860s grace would cost half an hour per deploy (external review)."""
    build = _build_script()
    drain_lines = [ln for ln in build.splitlines() if "wait_for_drain " in ln]
    # One helper definition plus the two call sites.
    calls = [ln for ln in drain_lines if "wait_for_drain group=" in ln]
    assert len(calls) == 2, drain_lines
    assert any("prod-webbackend" in ln for ln in calls)
    assert any("temporal-appeal-worker" in ln for ln in calls)
    assert not any("fax" in ln for ln in calls)
    # And it is a real gate: failing to drain stops the deploy.
    fn = _extract_shell_function(build, "wait_for_drain")
    assert "exit 1" in fn


def test_drained_deployments_actually_carry_the_labels_the_drain_selects_on():
    """The drain keys on `group=` labels, which are NARROWER than the web
    Deployment's own selector (`app: fight-health-insurance-prod`, which also
    matches the worker pods and so cannot be used here). That is only sound
    while every pod of those Deployments carries the group label -- if one
    ever stopped, `rollout status` could finish and the drain would see an
    empty list while an old pod was still inside its grace period (external
    review). Pin the labels so that becomes a CI failure, not a silent hole."""
    import pathlib

    import yaml

    root = pathlib.Path(__file__).resolve().parents[2]
    build = _build_script()

    def pod_template_labels(path, deployment_name):
        raw = (root / path).read_text().replace("${FHI_BASE}:${FHI_VERSION}", "image")
        for doc in yaml.safe_load_all(raw):
            if (
                doc
                and doc.get("kind") == "Deployment"
                and doc["metadata"]["name"] == deployment_name
            ):
                return doc["spec"]["template"]["metadata"]["labels"]
        raise AssertionError(f"{deployment_name} not found in {path}")

    for path, name, label in (
        ("k8s/deploy.yaml", "web", "fight-health-insurance-prod-webbackend"),
        (
            "k8s/temporal/appeal-worker.yaml",
            "fhi-appeal-worker",
            "fight-health-insurance-prod-temporal-appeal-worker",
        ),
    ):
        assert pod_template_labels(path, name).get("group") == label, name
        assert f"wait_for_drain group={label} " in build


def test_ray_gate_requires_the_pre_delete_pods_to_be_gone():
    """`kubectl delete` is background-cascading: the RayCluster object goes
    before its pods do, and both generations carry the same ray.io/cluster
    label -- so an old Ready pod could satisfy the readiness gate while still
    running SpeculativeAppealsActor (external review)."""
    build = _build_script()
    snapshot_at = build.index("RAY_PODS_BEFORE=")
    delete_at = build.index("delete raycluster")
    assert snapshot_at < delete_at, "the snapshot has to precede the delete"
    assert "ray_old_pods_remaining" in build
    # The old delete masked every failure -- 403, timeout, API error -- as an
    # absent cluster.
    assert 'delete raycluster -n totallylegitco raycluster-kuberay --ignore-not-found' in build
    assert 'raycluster-kuberay || echo' not in build


def test_ray_terminating_check_does_not_discard_kubectls_exit_status():
    """`[ -n "$(terminating_pods ...)" ]` throws away the exit status: a failed
    list is an empty string, which reads as "nothing is terminating" and opens
    the gate while old Ray pods can still write (external review)."""
    build = _build_script()
    code = [ln for ln in build.splitlines() if not ln.lstrip().startswith("#")]
    assert not any(
        'terminating_pods' in ln and '-n "$(' in ln for ln in code
    ), "a terminating_pods read is still inside a bare test substitution"
    assert 'if ! ray_terminating="$(terminating_pods' in build
    at = build.index('if ! ray_terminating="$(terminating_pods')
    assert "exit 1" in build[at : at + 300]


def test_ray_readiness_counts_pods_against_the_size_the_cluster_should_reach():
    """`kubectl wait` resolves its selector once per invocation, so a wait
    begun when only the head pod existed would never look at the worker pods
    created after it -- and the head is not what runs SpeculativeAppealsActor.
    KubeRay also clamps a worker group UP to minReplicas, so k8s/ray/cluster.yaml
    (`replicas: 1`, `minReplicas: 2`) really means head + 2 workers
    (external review)."""
    import os
    import pathlib
    import subprocess
    import tempfile

    build = _build_script()
    fns = "\n".join(
        _extract_shell_function(build, name)
        for name in ("ray_expected_pods", "ray_ready_pods")
    )

    def expected_for(replicas: str, min_replicas: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            shim = pathlib.Path(tmp) / "kubectl"
            shim.write_text(
                "#!/bin/bash\n"
                'for a in "$@"; do case "$a" in\n'
                f'  *minReplicas*) echo -n "{min_replicas}"; exit 0 ;;\n'
                f'  *workerGroupSpecs*replicas*) echo -n "{replicas}"; exit 0 ;;\n'
                "esac; done\n"
                "exit 0\n"
            )
            shim.chmod(0o755)
            env = {**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}"}
            r = subprocess.run(
                ["bash", "-c", "set -e\nKGET=(kubectl)\n" + fns + "\nray_expected_pods\n"],
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
            )
            assert r.returncode == 0, r.stderr
            return r.stdout

    # The real manifest: one head plus a group clamped from 1 up to 2.
    assert expected_for("1", "2") == "3"
    # No clamp needed.
    assert expected_for("3", "1") == "4"
    # Several worker groups, each clamped independently.
    assert expected_for("1 5", "2 2") == "8"
    # No worker groups at all: the head alone.
    assert expected_for("", "") == "1"

    # And the gate compares the two rather than accepting any Ready pod.
    assert '[ "$ray_ready" -ge "$ray_expected" ] && break' in build
    at = build.index("only $ray_ready of $ray_expected pods Ready")
    assert "exit 1" in build[at : at + 200]


def test_skip_backfill_still_gates_the_rollouts():
    """--skip-backfill drops the Job and the drain that only the Job needs; a
    Deployment that never finishes rolling is a broken deploy either way, so
    the rollout waits stay. The help text has to say so."""
    import pathlib
    import subprocess

    build = _build_script()
    rollout_at = build.index("# ROLLOUT GATE")
    backfill_at = build.index('if [ "$SKIP_BACKFILL" = true ]')
    assert rollout_at < backfill_at
    # The rollout gate answers only to --skip-journey-gates.
    rollout_block = build[rollout_at:backfill_at]
    assert "SKIP_JOURNEY_GATES" in rollout_block
    assert "SKIP_BACKFILL" not in rollout_block

    root = pathlib.Path(__file__).resolve().parents[2]
    helped = subprocess.run(
        ["bash", str(root / "scripts" / "build.sh"), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert helped.returncode == 0, helped.stderr
    assert "Rollout waits still run" in helped.stdout


def test_crd_probes_do_not_read_an_api_error_as_a_missing_operator():
    """`kubectl get crd X >/dev/null 2>&1` reads a 403 or an API blip as "not
    installed", silently skipping the PodMonitors and alert rules while
    printing a message blaming the cluster -- the same no-metrics gap the
    apply ordering exists to close (external review)."""
    build = _build_script()
    code = [ln for ln in build.splitlines() if not ln.lstrip().startswith("#")]
    assert not any("get crd" in ln and "2>&1; then" in ln for ln in code), (
        "a CRD probe is still swallowing errors"
    )
    assert build.count("if crd_present ") == 4
    fn = _extract_shell_function(build, "crd_present")
    assert "not found" in fn and "exit 1" in fn


def test_deploy_script_enables_pipefail():
    """Every pipe here is `envsubst < manifest | kubectl apply -f -`. Without
    pipefail only kubectl's status counts, so an envsubst that died partway
    through a multi-document manifest would apply the prefix it had already
    emitted and report success (external review)."""
    build = _build_script()
    assert "set -o pipefail" in build


def test_gate_kubectl_calls_carry_a_request_timeout():
    """kubectl's default --request-timeout is 0, i.e. wait forever, so one
    stuck API request would hang the deploy past whatever budget the polls
    claim to enforce (external review)."""
    build = _build_script()
    assert "KGET=(kubectl --request-timeout=" in build
    # ...and the blocking commands deliberately do NOT carry one. They take
    # their own --timeout, and a per-request bound aborts an operation that is
    # progressing normally -- on older kubectl clients `rollout status` exits
    # when its watch request expires rather than re-establishing it
    # (external review). `kubectl apply` is in the same category.
    for call in ("rollout status deployment", "delete raycluster", "delete job"):
        lines = [
            ln.strip()
            for ln in build.splitlines()
            if call in ln and not ln.lstrip().startswith("#")
        ]
        assert lines, call
        for line in lines:
            assert "--request-timeout=" not in line, line
    # Each of them still needs its OWN bound, or there is none at all:
    # `kubectl delete` waits for finalizers by default and --timeout=0 means
    # "work it out from the object", which for a waited delete is effectively
    # forever (external review).
    for call, bound in (
        ("rollout status deployment", "--timeout=15m"),
        ("delete raycluster", "--timeout=5m"),
        ("delete job", "--timeout=2m"),
    ):
        lines = [
            ln
            for ln in build.splitlines()
            if call in ln and not ln.lstrip().startswith("#")
        ]
        assert lines, call
        for line in lines:
            assert bound in line, line


def test_backfill_job_runs_non_root_with_a_read_only_root_filesystem():
    """The Job carries both production secret sets, so it gets the same
    posture the web-extralink-prefetch Job already proves for this image:
    non-root, read-only root filesystem, writable /tmp (external review)."""
    import pathlib

    import yaml

    root = pathlib.Path(__file__).resolve().parents[2]
    raw = (root / "k8s" / "temporal" / "backfill-fingerprints-job.yaml").read_text()
    job = yaml.safe_load(raw.replace("${FHI_BASE}:${FHI_VERSION}", "image"))
    pod = job["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["runAsUser"] == 1000
    container = pod["containers"][0]["securityContext"]
    assert container["readOnlyRootFilesystem"] is True
    assert container["allowPrivilegeEscalation"] is False
    assert container["capabilities"]["drop"] == ["ALL"]
    assert container["seccompProfile"]["type"] == "RuntimeDefault"
    # A read-only root needs scratch space, exactly as the prefetch Job does.
    assert {"name": "tmp", "mountPath": "/tmp"} in pod["containers"][0]["volumeMounts"]
    assert any(v["name"] == "tmp" and "emptyDir" in v for v in pod["volumes"])
