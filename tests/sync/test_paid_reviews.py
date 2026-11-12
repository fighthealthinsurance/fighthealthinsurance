"""Paid Arena reviewers through RentAHuman (paid_reviews.py): the join link,
tagging and counting paid votes, the completion code, and checking the
submissions that come back. RentAHuman itself is never called: the client
is a mock."""

import datetime
import hashlib
import hmac
import json
import time
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from fighthealthinsurance import paid_reviews
from fighthealthinsurance.chooser_tokens import candidate_token
from fighthealthinsurance.models import (
    ChooserCandidate,
    ChooserTask,
    ChooserVote,
    PaidReviewBounty,
    PaidReviewSession,
)

VOTE_URL = "/ziggy/rest/chooser/vote/"
GOOD_REASON = "Draft A answers the denial reason with the case facts."


def make_bounty(**overrides) -> PaidReviewBounty:
    fields = dict(
        join_token=paid_reviews.new_join_token(),
        rah_bounty_id="bounty-1",
        title="Judge appeals",
        price_dollars=Decimal("5"),
        spots=3,
        reviews_required=2,
        status="open",
    )
    fields.update(overrides)
    return PaidReviewBounty.objects.create(**fields)


def make_task() -> tuple:
    task = ChooserTask.objects.create(task_type="appeal", status="READY")
    candidates = [
        ChooserCandidate.objects.create(
            task=task,
            candidate_index=i,
            kind="appeal_letter",
            model_name=f"model-{i}",
            content=f"Draft {i}",
        )
        for i in range(2)
    ]
    return task, candidates


def make_vote(run, seconds=120, rationale=GOOD_REASON, signals=None) -> ChooserVote:
    task, (first, second) = make_task()
    vote = ChooserVote.objects.create(
        task=task,
        chosen_candidate=first,
        presented_candidate_ids=[first.id, second.id],
        rationale=rationale,
        served_at=timezone.now() - datetime.timedelta(seconds=seconds),
        review_signals=signals or {},
        session_key=run.session_key,
        paid_review=run,
    )
    return vote


class JoinLinkTest(TestCase):
    def test_join_link_puts_the_session_in_a_paid_run_and_opens_the_arena(self):
        bounty = make_bounty()
        response = self.client.get(reverse("paid_review_join", args=[bounty.join_token]))
        self.assertRedirects(response, reverse("chooser"), fetch_redirect_response=False)
        run = PaidReviewSession.objects.get(bounty=bounty)
        self.assertEqual(self.client.session[paid_reviews.SESSION_KEY], run.id)

    def test_opening_the_link_twice_keeps_one_run(self):
        bounty = make_bounty()
        url = reverse("paid_review_join", args=[bounty.join_token])
        self.client.get(url)
        self.client.get(url)
        self.assertEqual(PaidReviewSession.objects.filter(bounty=bounty).count(), 1)

    def test_closed_bounty_link_says_the_task_closed(self):
        bounty = make_bounty(status="closed")
        response = self.client.get(reverse("paid_review_join", args=[bounty.join_token]))
        self.assertEqual(response.status_code, 410)

    def test_closed_bounty_link_starts_no_run(self):
        bounty = make_bounty(status="closed")
        self.client.get(reverse("paid_review_join", args=[bounty.join_token]))
        self.assertFalse(PaidReviewSession.objects.exists())

    def test_arena_page_carries_the_paid_progress(self):
        bounty = make_bounty()
        self.client.get(reverse("paid_review_join", args=[bounty.join_token]))
        with mock.patch("fighthealthinsurance.chooser_tasks.trigger_prefill_async"):
            page = self.client.get(reverse("chooser")).content.decode()
        self.assertIn('data-paid-required="2"', page)

    def test_volunteer_arena_page_has_no_paid_progress(self):
        with mock.patch("fighthealthinsurance.chooser_tasks.trigger_prefill_async"):
            page = self.client.get(reverse("chooser")).content.decode()
        self.assertNotIn("data-paid-required", page)


class PaidVoteApiTest(TestCase):
    def setUp(self):
        self.bounty = make_bounty()
        self.client.get(reverse("paid_review_join", args=[self.bounty.join_token]))
        self.run = PaidReviewSession.objects.get(bounty=self.bounty)

    def _vote(self, seconds_ago: int, rationale: str = GOOD_REASON):
        task, (first, second) = make_task()
        session = self.client.session
        session["chooser_served_at"] = {
            str(task.id): (
                timezone.now() - datetime.timedelta(seconds=seconds_ago)
            ).isoformat()
        }
        session.save()
        return self.client.post(
            VOTE_URL,
            data=json.dumps(
                {
                    "task_id": task.id,
                    "chosen_candidate_id": candidate_token(first.id),
                    "presented_candidate_ids": [
                        candidate_token(first.id),
                        candidate_token(second.id),
                    ],
                    "rationale": rationale,
                }
            ),
            content_type="application/json",
        )

    def test_paid_vote_is_tagged_with_the_run(self):
        self._vote(120)
        self.assertEqual(ChooserVote.objects.get().paid_review_id, self.run.id)

    def test_careful_vote_counts_toward_the_run(self):
        progress = self._vote(120).json()["paid_review"]
        self.assertEqual((progress["counted"], progress["last_vote_counted"]), (1, True))

    def test_rushed_vote_does_not_count_and_says_why(self):
        progress = self._vote(5).json()["paid_review"]
        self.assertIn("too quick", progress["note"])

    def test_completion_code_is_shown_after_enough_counted_votes(self):
        self._vote(120)
        progress = self._vote(120).json()["paid_review"]
        self.assertRegex(progress["completion_code"], paid_reviews.CODE_RE)

    def test_volunteer_vote_response_has_no_paid_progress(self):
        session = self.client.session
        del session[paid_reviews.SESSION_KEY]
        session.save()
        self.assertNotIn("paid_review", self._vote(120).json())


class VoteCountsTest(TestCase):
    def setUp(self):
        self.run = PaidReviewSession.objects.create(
            bounty=make_bounty(), session_key="s1"
        )

    def test_vote_without_a_reason_does_not_count(self):
        self.assertFalse(paid_reviews.vote_counts(make_vote(self.run, rationale=""))[0])

    def test_vote_from_an_automated_browser_does_not_count(self):
        vote = make_vote(self.run, signals={"webdriver": True})
        self.assertFalse(paid_reviews.vote_counts(vote)[0])

    def test_vote_with_no_recorded_serve_does_not_count(self):
        vote = make_vote(self.run)
        vote.served_at = None
        self.assertFalse(paid_reviews.vote_counts(vote)[0])

    def test_code_is_not_issued_before_the_required_count(self):
        make_vote(self.run)
        self.assertEqual(paid_reviews.progress(self.run)["completion_code"], "")

    def test_code_once_issued_stays_the_same(self):
        make_vote(self.run)
        make_vote(self.run)
        first = paid_reviews.progress(self.run)["completion_code"]
        make_vote(self.run)
        self.assertEqual(paid_reviews.progress(self.run)["completion_code"], first)


class ProcessSubmissionTest(TestCase):
    def setUp(self):
        self.bounty = make_bounty()
        self.run = PaidReviewSession.objects.create(
            bounty=self.bounty,
            session_key="s1",
            completion_code="FHI-ABCD-EFGH",
            status="code_issued",
        )
        self.client_mock = mock.Mock()

    def _submit(self, text: str, submission_id: str = "sub-1") -> str:
        return paid_reviews.process_submission(
            self.bounty,
            {"id": submission_id, "evidence": [{"type": "text", "content": text}]},
            self.client_mock,
        )

    def test_valid_code_is_approved(self):
        self.assertEqual(self._submit("my code: fhi-abcd-efgh"), "approved")

    def test_valid_code_approval_is_sent_to_rentahuman(self):
        self._submit("FHI-ABCD-EFGH")
        self.client_mock.review_submission.assert_called_once_with(
            "bounty-1", "sub-1", "approve", mock.ANY
        )

    def test_valid_code_claims_the_run_for_the_submission(self):
        self._submit("FHI-ABCD-EFGH")
        self.run.refresh_from_db()
        self.assertEqual(
            (self.run.rah_submission_id, self.run.status), ("sub-1", "approved")
        )

    def test_code_used_by_another_submission_gets_a_redo(self):
        self._submit("FHI-ABCD-EFGH", "sub-1")
        self._submit("FHI-ABCD-EFGH", "sub-2")
        self.assertIn(
            "already used", self.client_mock.review_submission.call_args.args[3]
        )

    def test_unknown_code_gets_a_redo(self):
        self.assertEqual(self._submit("FHI-ZZZZ-ZZZZ"), "redo requested")

    def test_submission_without_a_code_gets_a_redo_with_the_join_link(self):
        self._submit("I did the reviews")
        self.assertIn(
            paid_reviews.join_url(self.bounty),
            self.client_mock.review_submission.call_args.args[3],
        )

    def test_code_from_another_bounty_gets_a_redo(self):
        other = make_bounty(rah_bounty_id="bounty-2")
        outcome = paid_reviews.process_submission(
            other, {"id": "sub-9", "text": "FHI-ABCD-EFGH"}, self.client_mock
        )
        self.assertEqual(outcome, "redo requested")

    def test_same_submission_seen_twice_is_answered_once(self):
        self._submit("FHI-ABCD-EFGH")
        self._submit("FHI-ABCD-EFGH")
        self.assertEqual(self.client_mock.review_submission.call_count, 1)

    def test_already_reviewed_submission_is_not_answered_again(self):
        paid_reviews.process_submission(
            self.bounty,
            {"id": "sub-1", "status": "redo_requested", "text": "no code"},
            self.client_mock,
        )
        self.client_mock.review_submission.assert_not_called()

    def test_failed_approval_is_retried_on_the_next_sight(self):
        self.client_mock.review_submission.side_effect = [
            paid_reviews.RentAHumanError("down"),
            None,
        ]
        with self.assertRaises(paid_reviews.RentAHumanError):
            self._submit("FHI-ABCD-EFGH")
        self.assertEqual(self._submit("FHI-ABCD-EFGH"), "already handled (approved)")


class SignatureTest(TestCase):
    SECRET = "whsec_test"

    def _header(self, body: bytes, sent_at: int) -> str:
        digest = hmac.new(
            self.SECRET.encode(), f"{sent_at}.".encode() + body, hashlib.sha256
        ).hexdigest()
        return f"t={sent_at},v1={digest}"

    def test_signed_body_passes(self):
        now = int(time.time())
        self.assertTrue(
            paid_reviews.signature_ok(self.SECRET, self._header(b"{}", now), b"{}")
        )

    def test_tampered_body_fails(self):
        now = int(time.time())
        self.assertFalse(
            paid_reviews.signature_ok(self.SECRET, self._header(b"{}", now), b"[]")
        )

    def test_stale_signature_fails(self):
        stale = int(time.time()) - 600
        self.assertFalse(
            paid_reviews.signature_ok(self.SECRET, self._header(b"{}", stale), b"{}")
        )


@override_settings(RENTAHUMAN_WEBHOOK_SECRET="whsec_test", RENTAHUMAN_API_KEY="rah_test")
class WebhookViewTest(TestCase):
    def setUp(self):
        cache.clear()
        self.bounty = make_bounty()
        PaidReviewSession.objects.create(
            bounty=self.bounty, session_key="s1", completion_code="FHI-ABCD-EFGH"
        )

    def _post(self, event: dict, secret: str = "whsec_test"):
        body = json.dumps(event).encode()
        sent_at = int(time.time())
        digest = hmac.new(
            secret.encode(), f"{sent_at}.".encode() + body, hashlib.sha256
        ).hexdigest()
        return self.client.post(
            reverse("rentahuman_webhook"),
            data=body,
            content_type="application/json",
            HTTP_X_RENTAHUMAN_SIGNATURE=f"t={sent_at},v1={digest}",
        )

    def _event(self, event_id: str = "evt-1") -> dict:
        return {
            "id": event_id,
            "type": "submission.submitted",
            "data": {"bountyId": "bounty-1", "submissionId": "sub-1"},
        }

    def test_unsigned_event_is_refused(self):
        response = self._post(self._event(), secret="whsec_wrong")
        self.assertEqual(response.status_code, 403)

    def test_submission_event_fetches_and_approves_the_submission(self):
        with mock.patch.object(
            paid_reviews.RentAHumanClient,
            "get_submission",
            return_value={"id": "sub-1", "text": "FHI-ABCD-EFGH"},
        ), mock.patch.object(
            paid_reviews.RentAHumanClient, "review_submission"
        ) as review:
            self._post(self._event())
        self.assertEqual(review.call_args.args[2], "approve")

    def test_submission_id_is_read_from_the_event_id_when_data_lacks_it(self):
        event = {
            "id": "submission.submitted:sub-7",
            "type": "submission.submitted",
            "data": {"bountyId": "bounty-1"},
        }
        with mock.patch.object(
            paid_reviews.RentAHumanClient,
            "get_submission",
            return_value={"text": "FHI-ABCD-EFGH"},
        ) as fetch, mock.patch.object(
            paid_reviews.RentAHumanClient, "review_submission"
        ):
            self._post(event)
        self.assertEqual(fetch.call_args.args, ("bounty-1", "sub-7"))

    def test_resubmission_after_a_redo_is_checked_again(self):
        answers = [{"id": "sub-1", "text": "no code"}, {"id": "sub-1", "text": "FHI-ABCD-EFGH"}]
        with mock.patch.object(
            paid_reviews.RentAHumanClient, "get_submission", side_effect=answers
        ), mock.patch.object(
            paid_reviews.RentAHumanClient, "review_submission"
        ) as review:
            self._post(self._event())
            self._post(self._event())
        self.assertEqual(
            [c.args[2] for c in review.call_args_list], ["request_redo", "approve"]
        )

    def test_other_events_are_acknowledged_and_ignored(self):
        response = self._post({"id": "evt-2", "type": "message.received", "data": {}})
        self.assertEqual(response.content, b"ignored")

    def test_rentahuman_outage_asks_for_a_retry(self):
        with mock.patch.object(
            paid_reviews.RentAHumanClient,
            "get_submission",
            side_effect=paid_reviews.RentAHumanError("down"),
        ):
            response = self._post(self._event())
        self.assertEqual(response.status_code, 502)


class LiveResponseShapeTest(TestCase):
    """Response shapes copied from RentAHuman's live API (Nov 2026)."""

    def test_webhook_endpoint_id_is_read_from_the_create_answer(self):
        answer = {
            "success": True,
            "endpoint": {
                "id": "8R5nKXHkeq3pIJE7m4MN",
                "url": "https://www.fighthealthinsurance.com/chooser/paid/webhook/rentahuman/",
                "events": ["submission.submitted"],
            },
            "secret": "whsec_example",
        }
        self.assertEqual(
            paid_reviews.find_id(answer, "id", "endpointId"), "8R5nKXHkeq3pIJE7m4MN"
        )

    def test_webhook_secret_is_read_from_the_create_answer(self):
        answer = {"success": True, "endpoint": {"id": "e1"}, "secret": "whsec_example"}
        self.assertEqual(paid_reviews.find_id(answer, "secret"), "whsec_example")

    def test_test_event_is_ignored(self):
        event = {
            "id": "webhook.test:xtXLX67TQJyQaK0ZeCfz:ddd8ce1f-ada3-4d85-afb5-3222c056077d",
            "type": "webhook.test",
            "created": "2026-11-12T02:00:48.280Z",
            "data": {"endpointId": "xtXLX67TQJyQaK0ZeCfz", "message": "test"},
        }
        self.assertEqual(paid_reviews.handle_event(event), "ignored")


class BountyPayloadTest(TestCase):
    def test_payload_links_the_join_url(self):
        bounty = make_bounty()
        payload = paid_reviews.bounty_payload(bounty, dry_run=True)
        self.assertIn(paid_reviews.join_url(bounty), payload["description"])

    def test_payload_is_a_remote_text_evidence_task(self):
        payload = paid_reviews.bounty_payload(make_bounty(), dry_run=True)
        self.assertEqual(
            (payload["evidenceTypes"], payload["location"]["isRemoteAllowed"]),
            (["text"], True),
        )

    def test_total_cost_adds_the_platform_fee(self):
        self.assertEqual(paid_reviews.total_cost(Decimal("5"), 10), Decimal("59.00"))

    def test_client_refuses_to_start_without_a_key(self):
        with self.assertRaises(paid_reviews.RentAHumanError):
            paid_reviews.RentAHumanClient()


class ReviewFollowUpTest(TestCase):
    """Fixes from the PR's code review."""

    def setUp(self):
        self.bounty = make_bounty()
        self.run = PaidReviewSession.objects.create(
            bounty=self.bounty, session_key="s1", completion_code="FHI-ABCD-EFGH"
        )

    def test_second_redo_on_a_resubmission_uses_a_new_idempotency_key(self):
        client = mock.Mock()
        for text in ("no code", "FHI-ZZZZ-ZZZZ"):
            paid_reviews.process_submission(
                self.bounty, {"id": "sub-1", "text": text}, client
            )
        attempts = [c.kwargs["attempt"] for c in client.review_submission.call_args_list]
        self.assertNotEqual(attempts[0], attempts[1])

    def test_lost_claim_to_the_same_submission_sends_no_redo(self):
        # The other path (webhook or poll) claimed sub-1 between the checks.
        PaidReviewSession.objects.filter(id=self.run.id).update(
            rah_submission_id="sub-1", status="verified"
        )
        client = mock.Mock()
        outcome = paid_reviews.process_submission(
            self.bounty, {"id": "sub-1", "text": "FHI-ABCD-EFGH"}, client
        )
        self.assertFalse(
            any(c.args[2] == "request_redo" for c in client.review_submission.call_args_list),
            outcome,
        )

    def test_example_code_in_the_bounty_text_is_not_a_code(self):
        payload = paid_reviews.bounty_payload(self.bounty, dry_run=True)
        self.assertEqual(paid_reviews.codes_in(payload), [])

    def test_earned_code_stays_visible_after_the_bounty_closes(self):
        self.bounty.status = "closed"
        self.bounty.save()
        request = mock.Mock(session={paid_reviews.SESSION_KEY: self.run.id})
        self.assertEqual(paid_reviews.session_for(request), self.run)

    def test_unfinished_run_keeps_going_after_the_bounty_closes(self):
        self.bounty.status = "closed"
        self.bounty.save()
        unfinished = PaidReviewSession.objects.create(bounty=self.bounty, session_key="s2")
        request = mock.Mock(session={paid_reviews.SESSION_KEY: unfinished.id})
        self.assertEqual(paid_reviews.session_for(request), unfinished)

    def test_non_json_answer_is_a_rentahuman_error(self):
        http = mock.Mock()
        http.request.return_value = mock.Mock(
            status_code=200, content=b"<html>", text="<html>"
        )
        http.request.return_value.json.side_effect = ValueError("not json")
        client = paid_reviews.RentAHumanClient(api_key="rah_test", session=http)
        with self.assertRaises(paid_reviews.RentAHumanError):
            client.wallet_balance()

    @override_settings(RENTAHUMAN_API_KEY="")
    def test_event_without_an_api_key_is_acknowledged_not_retried(self):
        event = {
            "id": "submission.submitted:sub-1",
            "type": "submission.submitted",
            "data": {"bountyId": "bounty-1", "submissionId": "sub-1"},
        }
        self.assertEqual(paid_reviews.handle_event(event), "ignored: no API key")


class CodeRabbitFollowUpTest(TestCase):
    """Fixes from CodeRabbit's review."""

    def setUp(self):
        self.bounty = make_bounty()
        self.run = PaidReviewSession.objects.create(bounty=self.bounty, session_key="s1")

    def test_chat_vote_does_not_count_toward_an_appeal_bounty(self):
        vote = make_vote(self.run)
        vote.task.task_type = "chat"
        self.assertFalse(paid_reviews.vote_counts(vote)[0])

    def test_one_review_bounty_meets_the_minimum_estimate(self):
        bounty = make_bounty(reviews_required=1, rah_bounty_id="bounty-short")
        payload = paid_reviews.bounty_payload(bounty, dry_run=True)
        self.assertGreaterEqual(payload["estimatedHours"], 0.083)

    def test_post_without_a_readable_id_stays_a_draft(self):
        from django.core.management import CommandError, call_command

        with mock.patch.object(
            paid_reviews.RentAHumanClient, "__init__", return_value=None
        ), mock.patch.object(
            paid_reviews.RentAHumanClient, "create_bounty", return_value={"ok": 1}
        ):
            with self.assertRaises(CommandError):
                call_command("post_paid_review_bounty", "--spots", "1", "--post")
        self.assertEqual(
            PaidReviewBounty.objects.exclude(id=self.bounty.id).get().status, "draft"
        )

    def test_retry_reuses_the_drafts_idempotency_key(self):
        from django.core.management import call_command

        draft = make_bounty(status="draft", rah_bounty_id="")
        with mock.patch.object(
            paid_reviews.RentAHumanClient, "__init__", return_value=None
        ), mock.patch.object(
            paid_reviews.RentAHumanClient,
            "create_bounty",
            return_value={"bounty": {"id": "rah-9"}},
        ) as create:
            call_command("post_paid_review_bounty", "--retry", str(draft.id))
        self.assertEqual(
            create.call_args.args[0]["idempotencyKey"],
            paid_reviews.bounty_payload(draft, dry_run=False)["idempotencyKey"],
        )

    def test_retry_opens_the_draft_once_the_id_is_known(self):
        from django.core.management import call_command

        draft = make_bounty(status="draft", rah_bounty_id="")
        with mock.patch.object(
            paid_reviews.RentAHumanClient, "__init__", return_value=None
        ), mock.patch.object(
            paid_reviews.RentAHumanClient,
            "create_bounty",
            return_value={"bounty": {"id": "rah-9"}},
        ):
            call_command("post_paid_review_bounty", "--retry", str(draft.id))
        draft.refresh_from_db()
        self.assertEqual((draft.status, draft.rah_bounty_id), ("open", "rah-9"))
