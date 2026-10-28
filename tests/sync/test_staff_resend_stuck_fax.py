"""Staff dashboard: re-send a fax whose vendor-send claim was stranded.

A worker that dies mid-send (OOM, eviction) leaves vendor_send_completed
True with no in-process handler alive to release it, and
send_fax_via_vendor then short-circuits on that claim forever. These cover
the two things that actually matter: staff can clear it, and nobody can use
it to fax an insurer twice.
"""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from fighthealthinsurance.helpers.fax_helpers import STAFF_RESEND_AFTER

from fighthealthinsurance.models import Denial, FaxesToSend


_START = "fighthealthinsurance.temporal_client.start_send_fax_workflow"
_RAY = "fighthealthinsurance.helpers.fax_helpers.fax_actor_ref"


# The sync conftest turns Temporal off around every test; the tests that
# expect a Temporal send turn it back on per method, which wins.
_TEMPORAL_ON = override_settings(TEMPORAL_ENABLED=True)


class ResendStuckFaxTest(TestCase):
    def setUp(self):
        self.client = Client()
        User = get_user_model()
        self.staff = User.objects.create_user(
            username="staffer", password="pw", is_staff=True
        )
        self.denial = Denial.objects.create(
            hashed_email="h", denial_text="denied", insurance_company="TestIns"
        )
        self.fax = FaxesToSend.objects.create(
            hashed_email="h",
            denial_id=self.denial,
            destination="15551234567",
            sent=True,
            fax_success=False,
            should_send=True,
            paid=True,
            vendor_send_completed=True,  # the stranded claim
        )
        self.url = "/timbit/help/resend_stuck_fax"

    def _login(self):
        self.client.force_login(self.staff)

    @_TEMPORAL_ON
    def test_releases_the_claim_and_starts_a_send(self):
        self._login()
        with patch(
            "fighthealthinsurance.temporal_client.start_send_fax_workflow"
        ) as start:
            start.return_value = "send-fax-abc"
            r = self.client.post(self.url, {"uuid": str(self.fax.uuid)})
        assert r.status_code == 200, r.content
        self.fax.refresh_from_db()
        assert self.fax.vendor_send_completed is False
        # An explicit resend must supersede any run still open for this fax,
        # or the deterministic workflow id makes this endpoint 502 in exactly
        # the case it exists for. Arguments: hashed email, uuid, delay_send,
        # force_restart.
        assert start.call_args.args == ("h", str(self.fax.uuid), False, True), (
            start.call_args
        )

    @_TEMPORAL_ON
    def test_refuses_a_fax_that_already_succeeded(self):
        """The stranded claim means the send was CLAIMED, not that it
        completed. fax_success is the only field that says it went through --
        so it, not the claim, is what guards against double-faxing."""
        FaxesToSend.objects.filter(pk=self.fax.pk).update(fax_success=True)
        self._login()
        with patch(
            "fighthealthinsurance.temporal_client.start_send_fax_workflow"
        ) as start:
            r = self.client.post(self.url, {"uuid": str(self.fax.uuid)})
        assert r.status_code == 409
        assert not start.called
        self.fax.refresh_from_db()
        # ...and the claim is left exactly as it was.
        assert self.fax.vendor_send_completed is True

    @_TEMPORAL_ON
    def test_blank_destination_is_refused(self):
        """Whitespace passes an `is None` check but precheck_fax treats it as
        missing, so we would report a resend that could never send."""
        FaxesToSend.objects.filter(pk=self.fax.pk).update(destination="   ")
        self._login()
        with patch(
            "fighthealthinsurance.temporal_client.start_send_fax_workflow"
        ) as start:
            r = self.client.post(self.url, {"uuid": str(self.fax.uuid)})
        assert r.status_code == 400
        assert not start.called
        self.fax.refresh_from_db()
        assert self.fax.vendor_send_completed is True

    @_TEMPORAL_ON
    def test_get_is_not_allowed(self):
        """A GET would let a crawler or a prefetch re-fax a patient's appeal."""
        self._login()
        with patch(
            "fighthealthinsurance.temporal_client.start_send_fax_workflow"
        ) as start:
            r = self.client.get(self.url, {"uuid": str(self.fax.uuid)})
        assert r.status_code in (405, 302), r.status_code
        assert not start.called

    @_TEMPORAL_ON
    def test_requires_staff(self):
        User = get_user_model()
        nonstaff = User.objects.create_user(username="rando", password="pw")
        self.client.force_login(nonstaff)
        with patch(
            "fighthealthinsurance.temporal_client.start_send_fax_workflow"
        ) as start:
            r = self.client.post(self.url, {"uuid": str(self.fax.uuid)})
        assert r.status_code in (302, 403), r.status_code
        assert not start.called

    def test_unknown_uuid_is_a_404_not_a_crash(self):
        self._login()
        r = self.client.post(
            self.url, {"uuid": "00000000-0000-0000-0000-000000000000"}
        )
        assert r.status_code == 404

    def _requested(self):
        """A row staging saved and marked paid, never attempted, no claim
        taken: what the staff page's "Requested, never sent" list posts."""
        return FaxesToSend.objects.create(
            hashed_email="h",
            denial_id=self.denial,
            destination="15551234567",
            paid=True,
            should_send=False,
        )

    @_TEMPORAL_ON
    def test_sends_a_requested_fax_that_never_started(self):
        requested = self._requested()
        self._login()
        with patch(_START) as start, patch(_RAY) as ray_ref:
            start.return_value = f"send-fax-{requested.uuid}"
            r = self.client.post(self.url, {"uuid": str(requested.uuid)})
        assert r.status_code == 200, r.content
        assert start.call_args.args == ("h", str(requested.uuid), False, True), (
            start.call_args
        )
        assert not ray_ref.get.do_send_fax.remote.called

    @override_settings(TEMPORAL_ENABLED=False)
    def test_sends_through_the_ray_fax_actor_when_temporal_is_off(self):
        requested = self._requested()
        self._login()
        with patch(_START) as start, patch(_RAY) as ray_ref:
            r = self.client.post(self.url, {"uuid": str(requested.uuid)})
        assert r.status_code == 200, r.content
        assert not start.called
        ray_ref.get.do_send_fax.remote.assert_called_once_with(
            "h", str(requested.uuid)
        )

    @_TEMPORAL_ON
    def test_falls_back_to_ray_when_temporal_will_not_start(self):
        """A first dispatch that failed often means Temporal is down, so the
        recovery button must not depend on it alone."""
        requested = self._requested()
        self._login()
        with patch(_START) as start, patch(_RAY) as ray_ref:
            start.side_effect = RuntimeError("frontend unreachable")
            r = self.client.post(self.url, {"uuid": str(requested.uuid)})
        assert r.status_code == 200, r.content
        ray_ref.get.do_send_fax.remote.assert_called_once_with(
            "h", str(requested.uuid)
        )

    def _attempt_started(self, ago, **fields):
        FaxesToSend.objects.filter(pk=self.fax.pk).update(
            attempting_to_send_as_of=timezone.now() - ago, **fields
        )

    @_TEMPORAL_ON
    def test_waits_while_the_last_send_may_still_be_dialing(self):
        """A send that hung keeps its claim through the workflow's drain;
        releasing it then would let two sends dial at once."""
        self._attempt_started(timedelta(minutes=40))
        self._login()
        with patch(_START) as start, patch(_RAY) as ray_ref:
            r = self.client.post(self.url, {"uuid": str(self.fax.uuid)})
        assert r.status_code == 409, r.status_code
        assert b"may still be running" in r.content
        assert not start.called
        assert not ray_ref.get.do_send_fax.remote.called
        self.fax.refresh_from_db()
        assert self.fax.vendor_send_completed is True

    @_TEMPORAL_ON
    def test_releases_a_claim_once_its_send_is_old_enough(self):
        self._attempt_started(STAFF_RESEND_AFTER + timedelta(minutes=1))
        self._login()
        with patch(_START) as start:
            r = self.client.post(self.url, {"uuid": str(self.fax.uuid)})
        assert r.status_code == 200, r.content
        assert start.called
        self.fax.refresh_from_db()
        assert self.fax.vendor_send_completed is False

    @_TEMPORAL_ON
    def test_a_failed_send_that_released_its_claim_can_be_sent_at_once(self):
        self._attempt_started(timedelta(minutes=5), vendor_send_completed=False)
        self._login()
        with patch(_START) as start:
            r = self.client.post(self.url, {"uuid": str(self.fax.uuid)})
        assert r.status_code == 200, r.content
        assert start.called

    @_TEMPORAL_ON
    def test_a_second_click_starts_nothing(self):
        self._login()
        with patch(_START) as start:
            first = self.client.post(self.url, {"uuid": str(self.fax.uuid)})
            second = self.client.post(self.url, {"uuid": str(self.fax.uuid)})
        assert first.status_code == 200, first.content
        assert second.status_code == 409, second.status_code
        assert start.call_count == 1

    @_TEMPORAL_ON
    def test_a_send_that_could_not_start_leaves_the_next_click_free(self):
        self._login()
        with patch(_START) as start, patch(_RAY) as ray_ref:
            start.side_effect = RuntimeError("frontend unreachable")
            ray_ref.get.do_send_fax.remote.side_effect = RuntimeError("no Ray")
            r = self.client.post(self.url, {"uuid": str(self.fax.uuid)})
        assert r.status_code == 502, r.status_code
        self.fax.refresh_from_db()
        assert self.fax.attempting_to_send_as_of is None

    @_TEMPORAL_ON
    def test_says_so_when_neither_temporal_nor_ray_can_start(self):
        requested = self._requested()
        self._login()
        with patch(_START) as start, patch(_RAY) as ray_ref:
            start.side_effect = RuntimeError("frontend unreachable")
            ray_ref.get.do_send_fax.remote.side_effect = RuntimeError("no Ray")
            r = self.client.post(self.url, {"uuid": str(requested.uuid)})
        assert r.status_code == 502, r.status_code
