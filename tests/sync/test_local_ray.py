"""Gating tests for the deliberate dev-parity Ray boot (local_ray).

These never boot a real cluster -- ray is mocked throughout. The real boot is
exercised by tests/sync-actor/test_local_ray_boot.py, where paying for Ray
startup is the suite's normal cost.
"""

import os
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.test import TestCase, override_settings

from fighthealthinsurance.local_ray import (
    LOCAL_RAY_ENV_VAR,
    LOCAL_RAY_POLLING_ENV_VAR,
    local_ray_enabled,
    maybe_init_local_ray,
)

_RAY = "fighthealthinsurance.local_ray.ray"
_RELAUNCH = "fighthealthinsurance.actor_health_status.relaunch_actors"


def _clean_env():
    """patch.dict context with the three relevant vars removed.

    tox runs with passenv = *, so FHI_LOCAL_RAY / FHI_LOCAL_RAY_POLLING /
    RAY_ADDRESS could leak in from the host and flip a branch under test;
    patch.dict restores the original environment on exit even for keys
    popped inside the block.
    """
    ctx = patch.dict(os.environ, {}, clear=False)
    ctx.start()
    os.environ.pop(LOCAL_RAY_ENV_VAR, None)
    os.environ.pop(LOCAL_RAY_POLLING_ENV_VAR, None)
    os.environ.pop("RAY_ADDRESS", None)
    return ctx


class LocalRayGatingTest(TestCase):
    """maybe_init_local_ray must boot only when deliberately enabled."""

    def setUp(self):
        self._env = _clean_env()
        self.addCleanup(self._env.stop)

    def _mock_ray(self, initialized=False):
        mock_ray = MagicMock()
        mock_ray.is_initialized.return_value = initialized
        return patch(_RAY, mock_ray)

    def test_test_configs_keep_the_startup_boot_off(self):
        # The suite runs under a Test* configuration; the Dev default must
        # not leak through the subclass chain (Test* subclass Dev).
        self.assertFalse(settings.RAY_LOCAL_DEV_CLUSTER)
        self.assertFalse(local_ray_enabled())

    def test_disabled_makes_no_ray_calls(self):
        with self._mock_ray() as mock_ray:
            self.assertFalse(maybe_init_local_ray())
        mock_ray.init.assert_not_called()

    @override_settings(RAY_LOCAL_DEV_CLUSTER=True)
    def test_dev_setting_boots_local_cluster(self):
        with self._mock_ray() as mock_ray:
            self.assertTrue(maybe_init_local_ray())
        mock_ray.init.assert_called_once()
        kwargs = mock_ray.init.call_args.kwargs
        self.assertEqual(kwargs["namespace"], "fhi")
        # Local boot passes the dev sizing args (vs. the attach branch).
        self.assertIn("object_store_memory", kwargs)

    def test_env_var_true_overrides_test_setting(self):
        os.environ[LOCAL_RAY_ENV_VAR] = "true"
        with self._mock_ray() as mock_ray:
            self.assertTrue(maybe_init_local_ray())
        mock_ray.init.assert_called_once()

    @override_settings(RAY_LOCAL_DEV_CLUSTER=True)
    def test_env_var_false_is_a_kill_switch_over_dev_setting(self):
        os.environ[LOCAL_RAY_ENV_VAR] = "false"
        with self._mock_ray() as mock_ray:
            self.assertFalse(maybe_init_local_ray())
        mock_ray.init.assert_not_called()

    def test_already_initialized_short_circuits_without_reinit(self):
        # Truthful even when the gate is off (e.g. a sync-actor test already
        # attached this process): Ray is up, so report it and touch nothing.
        with self._mock_ray(initialized=True) as mock_ray:
            self.assertTrue(maybe_init_local_ray())
        mock_ray.init.assert_not_called()

    @override_settings(RAY_LOCAL_DEV_CLUSTER=True)
    def test_real_ray_address_attaches_without_local_sizing_args(self):
        # Sizing kwargs are only legal when starting a new local cluster;
        # with a real address ray.init must be left to attach.
        os.environ["RAY_ADDRESS"] = "ray://cluster:10001"
        with self._mock_ray() as mock_ray:
            self.assertTrue(maybe_init_local_ray())
        kwargs = mock_ray.init.call_args.kwargs
        self.assertEqual(kwargs["namespace"], "fhi")
        self.assertNotIn("object_store_memory", kwargs)
        self.assertNotIn("include_dashboard", kwargs)

    @override_settings(RAY_LOCAL_DEV_CLUSTER=True)
    def test_ray_address_local_takes_the_boot_branch(self):
        # "local" is Ray's spelling of "start a brand-new local cluster"
        # (mirrors base_actor_ref.ray_cluster_available), so the dev sizing
        # args apply.
        os.environ["RAY_ADDRESS"] = "local"
        with self._mock_ray() as mock_ray:
            self.assertTrue(maybe_init_local_ray())
        self.assertIn("object_store_memory", mock_ray.init.call_args.kwargs)

    @override_settings(RAY_LOCAL_DEV_CLUSTER=True)
    def test_boot_failure_is_swallowed_and_reports_false(self):
        # The server must still start without a cluster; dispatch sites then
        # use their non-Ray fallbacks.
        with self._mock_ray() as mock_ray:
            mock_ray.init.side_effect = RuntimeError("no cluster for you")
            self.assertFalse(maybe_init_local_ray())


class LocalRayPollingOptInTest(TestCase):
    """The polling-actor fleet launches only on explicit opt-in."""

    def setUp(self):
        self._env = _clean_env()
        self.addCleanup(self._env.stop)
        os.environ[LOCAL_RAY_ENV_VAR] = "true"

    def _boot(self):
        mock_ray = MagicMock()
        mock_ray.is_initialized.return_value = False
        with patch(_RAY, mock_ray), patch(_RELAUNCH) as mock_relaunch:
            mock_relaunch.return_value = {
                "email_polling_actor": {"status": "launched"}
            }
            self.assertTrue(maybe_init_local_ray())
        return mock_relaunch

    def test_polling_actors_not_launched_by_default(self):
        self._boot().assert_not_called()

    def test_polling_actors_launched_when_opted_in(self):
        os.environ[LOCAL_RAY_POLLING_ENV_VAR] = "true"
        mock_relaunch = self._boot()
        mock_relaunch.assert_called_once_with(force=False)

    def test_polling_launch_failure_does_not_fail_the_boot(self):
        os.environ[LOCAL_RAY_POLLING_ENV_VAR] = "true"
        mock_ray = MagicMock()
        mock_ray.is_initialized.return_value = False
        with patch(_RAY, mock_ray), patch(
            _RELAUNCH, side_effect=RuntimeError("actors sad")
        ):
            self.assertTrue(maybe_init_local_ray())
