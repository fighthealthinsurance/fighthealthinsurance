"""Role selection for ``run_temporal_worker``: the queue split must fail
loudly on a bad role and never silently host the wrong queues."""

import asyncio
import os
from unittest.mock import patch

import pytest
from django.core.management.base import CommandError

from fighthealthinsurance.management.commands.run_temporal_worker import (
    QUEUE_ROLES,
    Command,
)


def test_known_roles_are_exactly_fax_appeal_all():
    assert QUEUE_ROLES == ("fax", "appeal", "all")


@patch.dict(os.environ, {"TEMPORAL_WORKER_QUEUES": "bogus"})
def test_bad_env_role_fails_before_connecting():
    """argparse validates --queues, but the env var path must be checked
    too -- a typo'd Deployment env must crash loudly, not default to
    hosting every queue."""
    with pytest.raises(CommandError):
        asyncio.run(Command()._run({}))
