"""Draft scores ride the appeal stream as {"type": "score"} frames.

Drives the REAL generator with only the model layer and the scorer's HTTP
call stubbed (same harness as test_generation_lease), because the promises
here are about the stream: a score frame names a row that was streamed, it
lands before the done frame, the row keeps the score, and every way scoring
can be off or broken leaves the letters exactly as they were.
"""

import asyncio
import json
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import TransactionTestCase, override_settings

from fighthealthinsurance.generate_appeal import GeneratedAppeal
from fighthealthinsurance.ml import letter_quality
from fighthealthinsurance.models import Denial, ProposedAppeal

ENABLED = dict(TYPESAFE_API_KEY="test-key", TYPESAFE_LETTER_RANKING_ENABLED=True)

LETTERS = [
    "Dear Reviewer, appeal draft one: my physician documented months of "
    "conservative treatment without improvement before this imaging.",
    "To the appeals board: the plan's own coverage policy states imaging is "
    "covered after failed conservative care, which my records demonstrate.",
]


def _drafts():
    return [
        GeneratedAppeal(text=t, model_name="fhi-internal", context_level="full")
        for t in LETTERS
    ]


def _payload(grounding=2, other=2):
    answers = {q: {"score": other} for q in letter_quality.SCORE_QUESTIONS}
    answers[letter_quality.GROUNDING_QUESTION] = {"score": grounding}
    return {"answers": answers, "usage": {"input_tokens": 10}}


def _frames(chunks):
    out = []
    for chunk in chunks:
        try:
            data = json.loads(chunk)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


class _StreamBase(TransactionTestCase):
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
        self.email = "rank@example.com"
        self.denial = Denial.objects.create(
            denial_id=9301,
            denial_text="Coverage for the requested MRI was denied as not medically necessary.",
            semi_sekret="sekret",
            hashed_email=Denial.get_hashed_email(self.email),
            gen_attempts=3,
        )

    def _stream(self, mock_gen):
        from fighthealthinsurance.common_view_logic import AppealsBackendHelper

        mock_gen.make_appeals.side_effect = lambda *a, **k: iter(_drafts())

        async def drive():
            chunks = []
            async for chunk in AppealsBackendHelper.generate_appeals(
                {
                    "denial_id": self.denial.denial_id,
                    "email": self.email,
                    "semi_sekret": "sekret",
                }
            ):
                chunks.append(chunk)
            return chunks

        return _frames(async_to_sync(drive)())


class TestScoresRideTheStream(_StreamBase):
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_each_streamed_draft_gets_a_score_frame_before_done(self, mock_gen):
        seen_docs = []

        async def fake_post(document, timeout):
            seen_docs.append(document)
            return _payload()

        with override_settings(**ENABLED), patch.object(letter_quality, "_post", fake_post):
            frames = self._stream(mock_gen)

        letters = [f for f in frames if "content" in f and f.get("id") not in (None, "unknown")]
        scores = [f for f in frames if f.get("type") == "score"]
        self.assertEqual(len(letters), 2)
        self.assertEqual({s["id"] for s in scores}, {l["id"] for l in letters})
        for s in scores:
            self.assertEqual(s["quality_score"], 1.0)
            self.assertEqual(s["grounding_score"], 2)

        done_at = next(i for i, f in enumerate(frames) if f.get("phase") == "done")
        last_score_at = max(i for i, f in enumerate(frames) if f.get("type") == "score")
        self.assertLess(last_score_at, done_at)

        # The scorer saw the pre-substitution draft and the denial, nothing else.
        self.assertEqual(len(seen_docs), 2)
        for doc in seen_docs:
            self.assertIn("THE DENIAL:\nCoverage for the requested MRI", doc)
            self.assertNotIn(self.email, doc)

    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_the_row_keeps_the_score(self, mock_gen):
        async def fake_post(document, timeout):
            return _payload(grounding=0, other=1)

        with override_settings(**ENABLED), patch.object(letter_quality, "_post", fake_post):
            self._stream(mock_gen)

        rows = list(ProposedAppeal.objects.filter(for_denial=self.denial, speculative=False))
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertAlmostEqual(row.quality_score, 3 / 8)
            self.assertEqual(row.grounding_score, 0)
            self.assertEqual(row.quality_scorer, letter_quality.SCORER)
            self.assertIsNotNone(row.quality_scored_at)


class TestNothingIdentifyingLeaves(_StreamBase):
    """The prompt asks the model to write the patient and professional INTO
    the letter, so the redaction is the promise, not the placeholders."""

    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_known_identifiers_are_redacted_before_scoring(self, mock_gen):
        from django.contrib.auth import get_user_model

        from fhi_users.models import PatientUser, ProfessionalUser

        User = get_user_model()
        patient_user = User.objects.create_user(
            username="jane", email="jane.doe@example.org", first_name="Jane", last_name="Doe"
        )
        patient = PatientUser.objects.create(user=patient_user, display_name="Jane Q. Doe")
        prof_user = User.objects.create_user(
            username="drsam", email="sam@clinic.example", first_name="Sam", last_name="Smith"
        )
        professional = ProfessionalUser.objects.create(
            user=prof_user, active=True, npi_number="1234567890", fax_number="(415) 555-0100",
            display_name="Sam Smith MD",
        )
        Denial.objects.filter(pk=self.denial.pk).update(
            patient_user=patient, primary_professional=professional,
            raw_email="jane.doe@example.org", claim_id="TLH-2026-0091827",
            denial_text="Member Jane Q. Doe (claim TLH-2026-0091827) was denied an MRI. "
            "Questions: jane.doe@example.org or 415-555-0100.",
        )
        leaky = [
            "Dear Reviewer, I am writing on behalf of my patient Jane Q. Doe regarding claim "
            "TLH-2026-0091827. Her physician, Sam Smith MD (NPI 1234567890, fax (415) 555-0100), "
            "documented months of conservative treatment without improvement before this imaging.",
            "To the appeals board: Ms. Doe's plan states imaging is covered after failed conservative "
            "care, which the records from Dr. Smith demonstrate; reply to jane.doe@example.org.",
        ]
        mock_gen.make_appeals.side_effect = lambda *a, **k: iter(
            [GeneratedAppeal(text=t, model_name="fhi-internal", context_level="full") for t in leaky]
        )
        seen = []

        async def fake_post(document, timeout):
            seen.append(document)
            return _payload()

        from fighthealthinsurance.common_view_logic import AppealsBackendHelper

        async def drive():
            async for _ in AppealsBackendHelper.generate_appeals(
                {"denial_id": self.denial.denial_id, "email": self.email, "semi_sekret": "sekret"}
            ):
                pass

        with override_settings(**ENABLED), patch.object(letter_quality, "_post", fake_post):
            async_to_sync(drive)()

        self.assertEqual(len(seen), 2)
        for doc in seen:
            for secret in ("Jane", "Doe", "Smith", "1234567890", "555-0100", "jane.doe@", "sam@", "TLH-2026-0091827"):
                self.assertNotIn(secret, doc)
            self.assertIn("[PATIENT_", doc)
            self.assertIn("[CLAIM_ID_", doc)

    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_redaction_failure_turns_scoring_off_for_the_run(self, mock_gen):
        with override_settings(**ENABLED), patch(
            "fighthealthinsurance.common_view_logic.scoring_redactions",
            side_effect=RuntimeError("relation walk failed"),
        ), patch.object(letter_quality, "_post") as post:
            frames = self._stream(mock_gen)
            post.assert_not_called()
        self.assertEqual([f for f in frames if f.get("type") == "score"], [])
        self.assertEqual(len([f for f in frames if "content" in f]), 2)


class TestSynthesisIsScoredToo(_StreamBase):
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_the_synthesized_draft_gets_its_score_frame_before_done(self, mock_gen):
        synthesized = (
            "Dear Reviewer, this combined appeal draws on my physician's documentation of "
            "months of conservative treatment and the plan's own coverage policy, which "
            "states imaging is covered after failed conservative care."
        )
        mock_gen.synthesize_appeals = AsyncMock(return_value=synthesized)

        async def fake_post(document, timeout):
            return _payload()

        with override_settings(**ENABLED), patch.object(letter_quality, "_post", fake_post):
            frames = self._stream(mock_gen)

        synth_row = ProposedAppeal.objects.get(for_denial=self.denial, synthesized=True)
        self.assertIsNotNone(synth_row.quality_score)
        done_at = next(i for i, f in enumerate(frames) if f.get("phase") == "done")
        synth_scores = [i for i, f in enumerate(frames) if f.get("type") == "score" and f["id"] == str(synth_row.id)]
        self.assertEqual(len(synth_scores), 1)
        self.assertLess(synth_scores[0], done_at)


class TestReServedDraftsAreScoredToo(_StreamBase):
    """A draft saved before scoring existed (or under an older rubric) must
    not sort below every fresh draft just for being older."""

    def _existing(self, text, **fields):
        return ProposedAppeal.objects.create(
            for_denial=self.denial, appeal_text=text, model_name="fhi-internal",
            speculative=False, **fields,
        )

    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_an_unscored_existing_draft_gets_scored_and_framed_before_done(self, mock_gen):
        row = self._existing(
            "Dear Reviewer, an earlier draft: my physician documented months of "
            "conservative treatment without improvement before this imaging was ordered."
        )

        async def fake_post(document, timeout):
            return _payload()

        with override_settings(**ENABLED), patch.object(letter_quality, "_post", fake_post):
            frames = self._stream(mock_gen)

        row.refresh_from_db()
        self.assertIsNotNone(row.quality_score)
        done_at = next(i for i, f in enumerate(frames) if f.get("phase") == "done")
        mine = [i for i, f in enumerate(frames) if f.get("type") == "score" and f["id"] == str(row.id)]
        self.assertEqual(len(mine), 1)
        self.assertLess(mine[0], done_at)

    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_an_older_rubric_score_is_neither_served_nor_kept(self, mock_gen):
        row = self._existing(
            "Dear Reviewer, an earlier draft: my physician documented months of "
            "conservative treatment without improvement before this imaging was ordered.",
            quality_score=0.1, grounding_score=0.0, quality_scorer="typesafe/speed_latest/rubric-0",
        )

        async def fake_post(document, timeout):
            return _payload()

        with override_settings(**ENABLED), patch.object(letter_quality, "_post", fake_post):
            frames = self._stream(mock_gen)

        letter = next(f for f in frames if f.get("id") == str(row.id) and "content" in f)
        self.assertNotIn("quality_score", letter, "a stale score must not ride the letter frame")
        row.refresh_from_db()
        self.assertEqual(row.quality_score, 1.0)
        self.assertEqual(row.quality_scorer, letter_quality.SCORER)


class TestInFlightGuard:
    def test_a_row_already_being_scored_on_this_worker_is_not_sent_again(self):
        async def run():
            started = asyncio.Event()
            release = asyncio.Event()

            async def slow():
                started.set()
                await release.wait()

            task = asyncio.create_task(slow())
            letter_quality.keep_alive(task, "42")
            await started.wait()
            assert letter_quality.in_flight("42")
            assert letter_quality.in_flight_task("42") is task, "a reconnect attaches to THIS task"
            assert not letter_quality.in_flight("43")
            release.set()
            await task
            assert not letter_quality.in_flight("42")

        asyncio.run(run())

    def test_finishing_an_older_task_does_not_forget_a_newer_one_for_the_same_row(self):
        async def run():
            gate_old, gate_new = asyncio.Event(), asyncio.Event()

            async def wait_on(gate):
                await gate.wait()

            old = asyncio.create_task(wait_on(gate_old))
            letter_quality.keep_alive(old, "7")
            new = asyncio.create_task(wait_on(gate_new))
            letter_quality.keep_alive(new, "7")  # replaces the slot
            gate_old.set()
            await old
            await asyncio.sleep(0)  # let the done-callback run
            assert letter_quality.in_flight_task("7") is new
            gate_new.set()
            await new
            await asyncio.sleep(0)
            assert letter_quality.in_flight_task("7") is None

        asyncio.run(run())


class TestScoringStaysOutOfTheWay(_StreamBase):
    def _assert_plain_stream(self, frames):
        letters = [f for f in frames if "content" in f]
        self.assertEqual(len(letters), 2)
        self.assertEqual([f for f in frames if f.get("type") == "score"], [])
        self.assertTrue(any(f.get("phase") == "done" for f in frames))
        for row in ProposedAppeal.objects.filter(for_denial=self.denial):
            self.assertIsNone(row.quality_score)

    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_off_by_default(self, mock_gen):
        with patch.object(letter_quality, "_post") as post:
            frames = self._stream(mock_gen)
            post.assert_not_called()
        self._assert_plain_stream(frames)

    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_no_external_consent_means_no_scoring_even_when_enabled(self, mock_gen):
        Denial.objects.filter(pk=self.denial.pk).update(use_external=False)
        with override_settings(**ENABLED), patch.object(letter_quality, "_post") as post:
            frames = self._stream(mock_gen)
            post.assert_not_called()
        self._assert_plain_stream(frames)

    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_a_dead_scorer_changes_nothing_for_the_reader(self, mock_gen):
        async def fake_post(document, timeout):
            raise letter_quality.LetterScoringError("HTTP 503")

        with override_settings(**ENABLED), patch.object(letter_quality, "_post", fake_post):
            frames = self._stream(mock_gen)
        self._assert_plain_stream(frames)

    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_a_slow_scorer_is_waited_for_once_not_per_drain(self, mock_gen):
        import time

        async def fake_post(document, timeout):
            await asyncio.sleep(30)
            return _payload()

        drain = 1.0
        started = time.monotonic()
        with override_settings(**ENABLED), patch.object(letter_quality, "_post", fake_post), patch.object(letter_quality, "DRAIN_SECONDS", drain):
            frames = self._stream(mock_gen)
        elapsed = time.monotonic() - started
        letters = [f for f in frames if "content" in f]
        self.assertEqual(len(letters), 2)
        self.assertEqual([f for f in frames if f.get("type") == "score"], [])
        self.assertTrue(any(f.get("phase") == "done" for f in frames))
        # Two drains (before synthesis and before done) share ONE deadline:
        # well under two full waits.
        self.assertLess(elapsed, drain * 1.8 + 3.0)
