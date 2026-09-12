"""finalize_fax must never re-create a fax row that was deleted while the
send was in flight (a delete-my-data request racing the fax worker)."""

from unittest import mock

from django.test import TestCase

from fighthealthinsurance import fax_send_core
from fighthealthinsurance.models import FaxesToSend


class FinalizeFaxDeletedRowTest(TestCase):
    def _fax(self):
        return FaxesToSend.objects.create(
            hashed_email="h",
            paid=True,
            email="a@b.com",
            appeal_text="x",
            name="Test",
            destination="(555) 555-0100",
        )

    @mock.patch("fighthealthinsurance.fax_send_core.send_fax_status_notification")
    @mock.patch("fighthealthinsurance.fax_send_core.EmailMultiAlternatives")
    def test_deleted_row_is_not_resurrected_and_nobody_is_notified(
        self, mock_email, mock_notify
    ):
        fax = self._fax()
        pk = fax.pk
        FaxesToSend.objects.filter(pk=pk).delete()  # the person asked to be forgotten
        fax_send_core.finalize_fax(fax, True, False)
        self.assertFalse(
            FaxesToSend.objects.filter(pk=pk).exists()
        )  # save() would INSERT
        mock_notify.assert_not_called()
        mock_email.assert_not_called()

    @mock.patch("fighthealthinsurance.fax_send_core.send_fax_status_notification")
    @mock.patch("fighthealthinsurance.fax_send_core.EmailMultiAlternatives")
    def test_present_row_is_marked_sent(self, mock_email, mock_notify):
        fax = self._fax()
        fax_send_core.finalize_fax(fax, True, False)
        fax.refresh_from_db()
        self.assertTrue(fax.sent)
        self.assertTrue(fax.fax_success)
        mock_notify.assert_called_once()
