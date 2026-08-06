"""Real-boot test for the dev-parity in-process Ray cluster.

The gating logic is covered with mocks in tests/sync/test_local_ray.py; this
suite pays for actual Ray startup anyway, so here we verify the exact
ray.init call maybe_init_local_ray makes (namespace, dashboard off, small
object store) is accepted by the pinned Ray version and yields a cluster the
dispatch gate recognizes.
"""

import os
from unittest.mock import patch

import ray
from django.test import SimpleTestCase

from fighthealthinsurance.base_actor_ref import ray_cluster_available
from fighthealthinsurance.local_ray import (
    LOCAL_RAY_ENV_VAR,
    LOCAL_RAY_POLLING_ENV_VAR,
    maybe_init_local_ray,
)


class TestLocalRayBoot(SimpleTestCase):
    def tearDown(self):
        if ray.is_initialized():
            ray.shutdown()

    def test_boot_passes_dispatch_gate_and_is_idempotent(self):
        # TestActor keeps RAY_LOCAL_DEV_CLUSTER off; force-enable via the env
        # override -- which also exercises that override path for real.
        with patch.dict(os.environ, {LOCAL_RAY_ENV_VAR: "true"}, clear=False):
            os.environ.pop("RAY_ADDRESS", None)
            # tox runs with passenv = *: a developer's exported
            # FHI_LOCAL_RAY_POLLING would otherwise make this real boot
            # launch the actual polling-actor fleet mid-suite.
            os.environ.pop(LOCAL_RAY_POLLING_ENV_VAR, None)
            self.assertTrue(maybe_init_local_ray())
            self.assertTrue(ray.is_initialized())
            # The per-task dispatch sites gate on this: after the deliberate
            # boot they must take the same actor paths as production.
            self.assertTrue(ray_cluster_available())
            # Second call short-circuits on the running cluster.
            self.assertTrue(maybe_init_local_ray())
