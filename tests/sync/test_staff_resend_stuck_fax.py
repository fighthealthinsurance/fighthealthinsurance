"""Staff dashboard: re-send a fax whose vendor-send claim was stranded.

A worker that dies mid-send (OOM, eviction) leaves vendor_send_completed
True with no in-process handler alive to release it, and
send_fax_via_vendor then short-circuits on that claim forever. These cover
the two things that actually matter: staff can clear it, and nobody can use
it to fax an insurer twice.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from fighthealthinsurance.models import Denial, FaxesToSend


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
        assert start.called
        # An explicit resend must supersede any run still open for this fax,
        # or the deterministic workflow id makes this endpoint 502 in exactly
        # the case it exists for.
        assert start.call_args.kwargs.get("force_restart") is True, start.call_args

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

    def test_get_is_not_allowed(self):
        """A GET would let a crawler or a prefetch re-fax a patient's appeal."""
        self._login()
        with patch(
            "fighthealthinsurance.temporal_client.start_send_fax_workflow"
        ) as start:
            r = self.client.get(self.url, {"uuid": str(self.fax.uuid)})
        assert r.status_code in (405, 302), r.status_code
        assert not start.called

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
