"""Shared setup for tests of pages cached as whole responses (StaticIshView)."""

import time
from datetime import date
from unittest import mock


def keep_page_day_far_from_midnight(testcase) -> None:
    """Pin ``page_day`` to early in a day for the rest of ``testcase``.

    A StaticIshView's max-age stops at the next Honolulu midnight, so a test
    asserting the full ``max-age=1800`` fails when the suite happens to run in
    the last half hour before it (10:00 UTC): CI saw ``max-age=497``. Call it
    from setUp; the patch is undone at cleanup.
    """
    patcher = mock.patch(
        "fighthealthinsurance.views.page_day",
        return_value=(date(2027, 1, 15), time.time() + 2 * 24 * 60 * 60),
    )
    patcher.start()
    testcase.addCleanup(patcher.stop)
