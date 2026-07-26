"""Unit tests for Dev's external-storage locations.

Base points EXTERNAL_STORAGE at ``/external_data``, which only exists inside
the container images that create it (k8s/Dockerfile) or where the shared PVC
is mounted. Pointing dev at it meant every listing (the staff status page's
storage check, the check_storage REST endpoint) raised FileNotFoundError and
every CombinedStorage save silently dropped that backend.
"""

import os

from fighthealthinsurance import settings as fhi_settings


def _dev_storage(tmp_path, attr, location_attr):
    """Build one of Dev's storage backends rooted under tmp_path."""
    cfg = fhi_settings.Dev()
    setattr(cfg, location_attr, os.path.join(str(tmp_path), "storage"))
    return getattr(cfg, attr)


class TestDevExternalStorageLocations:
    """The DEFAULT_* constants are asserted rather than the effective
    locations: the latter are read from the environment at import time and tox
    runs with ``passenv = *``, so a host EXTERNAL_STORAGE_LOCATION would
    otherwise decide whether these pass."""

    def test_dev_default_is_not_the_container_only_root_path(self):
        assert fhi_settings.Dev.DEFAULT_EXTERNAL_STORAGE_LOCATION != "/external_data"
        assert fhi_settings.Prod.EXTERNAL_STORAGE_LOCATION == "/external_data"

    def test_dev_defaults_are_absolute_so_cwd_does_not_move_them(self):
        assert os.path.isabs(fhi_settings.Dev.DEFAULT_EXTERNAL_STORAGE_LOCATION)
        assert os.path.isabs(fhi_settings.Dev.DEFAULT_EXTERNAL_STORAGE_LOCATION_B)

    def test_locations_take_the_env_override_and_fall_back_to_the_defaults(self):
        for env_var, default_attr, location_attr in (
            (
                "EXTERNAL_STORAGE_LOCATION",
                "DEFAULT_EXTERNAL_STORAGE_LOCATION",
                "EXTERNAL_STORAGE_LOCATION",
            ),
            (
                "EXTERNAL_STORAGE_LOCATION_B",
                "DEFAULT_EXTERNAL_STORAGE_LOCATION_B",
                "EXTERNAL_STORAGE_LOCATION_B",
            ),
        ):
            expected = os.environ.get(env_var, getattr(fhi_settings.Dev, default_attr))
            assert getattr(fhi_settings.Dev, location_attr) == expected

    def test_external_storage_is_created_and_listable(self, tmp_path):
        storage = _dev_storage(
            tmp_path, "EXTERNAL_STORAGE", "EXTERNAL_STORAGE_LOCATION"
        )
        assert storage.listdir("./") == ([], [])

    def test_external_storage_b_is_created_and_listable(self, tmp_path):
        storage = _dev_storage(
            tmp_path, "EXTERNAL_STORAGE_B", "EXTERNAL_STORAGE_LOCATION_B"
        )
        assert storage.listdir("./") == ([], [])
