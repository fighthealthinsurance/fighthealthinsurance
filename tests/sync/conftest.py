"""Sync tests never reach a live Temporal.

The save paths these tests drive (update_denial, record_intent, deliver)
would connect to whatever TEMPORAL_* the invoking environment names, and tox
passes the environment through. Pinned off here for every sync test; the
temporal tox env is where that integration is exercised.
"""

import pytest
from django.test import override_settings


@pytest.fixture(autouse=True)
def _no_live_temporal():
    with override_settings(
        TEMPORAL_ENABLED=False,
        TEMPORAL_APPEAL_JOURNEY_ENABLED=False,
        TEMPORAL_INTAKE_JOURNEY_ENABLED=False,
    ):
        yield
