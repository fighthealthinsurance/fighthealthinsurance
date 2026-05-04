"""Test the UCR fire-and-forget hook in DenialCreatorHelper.extract_entity.

The hook should call dispatch_ucr_refresh only when the denial text matches
all three OON-under-reimbursement signals; otherwise it's a no-op.
"""

from unittest.mock import patch

import pytest
from django.test import TransactionTestCase

from fighthealthinsurance.common_view_logic import DenialCreatorHelper
from fighthealthinsurance.models import Denial

_OON_TEXT = (
    "Your claim was processed as out-of-network. The allowed amount was "
    "limited to the usual and customary rate, leaving a balance owed."
)
_NON_OON_TEXT = "The procedure is not medically necessary per policy XYZ-123."


@pytest.mark.django_db
class MaybeDispatchUcrTests(TransactionTestCase):
    def _make_denial(self, text: str) -> Denial:
        return Denial.objects.create(hashed_email="hash:test", denial_text=text)

    def test_dispatches_when_all_three_signals_match(self):
        denial = self._make_denial(_OON_TEXT)
        with patch("fighthealthinsurance.ucr_helper.dispatch_ucr_refresh") as dispatch:
            import asyncio

            asyncio.run(DenialCreatorHelper._maybe_dispatch_ucr(denial.pk))
        dispatch.assert_called_once_with(denial.pk)

    def test_skips_when_signals_missing(self):
        denial = self._make_denial(_NON_OON_TEXT)
        with patch("fighthealthinsurance.ucr_helper.dispatch_ucr_refresh") as dispatch:
            import asyncio

            asyncio.run(DenialCreatorHelper._maybe_dispatch_ucr(denial.pk))
        dispatch.assert_not_called()

    def test_swallows_dispatch_failures(self):
        denial = self._make_denial(_OON_TEXT)
        with patch(
            "fighthealthinsurance.ucr_helper.dispatch_ucr_refresh",
            side_effect=RuntimeError("boom"),
        ):
            import asyncio

            # Should not raise.
            asyncio.run(DenialCreatorHelper._maybe_dispatch_ucr(denial.pk))
