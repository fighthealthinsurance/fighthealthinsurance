"""Unit tests for Prod's DATABASES property: pg pool + connection knobs.

Every test configuration runs sqlite, so this is the only CI coverage the
production Postgres connection settings get -- the original pooling
misconfig ("pool" at a nesting level Django never reads, fixed in #878)
shipped precisely because nothing exercised this property.
"""

import os
from unittest import mock

import pytest

from fighthealthinsurance import settings as fhi_settings


def _prod_default_db(env: dict) -> dict:
    """Evaluate Prod().DATABASES["default"] under exactly the given env."""
    with mock.patch.dict(os.environ, env, clear=True):
        return fhi_settings.Prod().DATABASES["default"]


class TestProdDatabasesUnpooled:
    def test_pool_absent_when_pg_use_pool_unset(self):
        db = _prod_default_db({})
        assert "pool" not in db["OPTIONS"]

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
    def test_pool_absent_for_falsey_pg_use_pool(self, value):
        db = _prod_default_db({"PG_USE_POOL": value})
        assert "pool" not in db["OPTIONS"]

    def test_engine_is_prometheus_postgres(self):
        db = _prod_default_db({})
        assert db["ENGINE"] == "django_prometheus.db.backends.postgresql"

    def test_connect_timeout_defaults_to_ten_seconds(self):
        db = _prod_default_db({})
        assert db["OPTIONS"]["connect_timeout"] == 10

    def test_connect_timeout_env_override(self):
        db = _prod_default_db({"PG_CONNECT_TIMEOUT": "5"})
        assert db["OPTIONS"]["connect_timeout"] == 5

    def test_connect_timeout_clamped_to_minimum_of_one(self):
        db = _prod_default_db({"PG_CONNECT_TIMEOUT": "0"})
        assert db["OPTIONS"]["connect_timeout"] == 1

    def test_connect_timeout_malformed_env_falls_back_to_default(self):
        db = _prod_default_db({"PG_CONNECT_TIMEOUT": "banana"})
        assert db["OPTIONS"]["connect_timeout"] == 10


class TestProdDatabasesPooled:
    @pytest.mark.parametrize("value", ["1", "true", "TRUE", " yes ", "on"])
    def test_pool_enabled_for_truthy_pg_use_pool(self, value):
        db = _prod_default_db({"PG_USE_POOL": value})
        assert "pool" in db["OPTIONS"]

    def test_pool_defaults(self):
        db = _prod_default_db({"PG_USE_POOL": "1"})
        assert db["OPTIONS"]["pool"] == {
            "name": "fhi-default",
            "min_size": 2,
            "max_size": 16,
            "timeout": 5,
            "max_waiting": 32,
            "max_lifetime": 1800,
            "max_idle": 300,
        }

    def test_pool_max_size_grows_to_min_size(self):
        # min > max would make psycopg_pool's ConnectionPool raise
        # ValueError lazily on first DB access (every request 500s
        # instead of a clean failure at boot); the settings clamp max up.
        db = _prod_default_db(
            {"PG_USE_POOL": "1", "PG_POOL_MIN_SIZE": "20", "PG_POOL_MAX_SIZE": "10"}
        )
        assert db["OPTIONS"]["pool"]["max_size"] == 20

    def test_pool_max_waiting_defaults_to_twice_max_size(self):
        db = _prod_default_db({"PG_USE_POOL": "1", "PG_POOL_MAX_SIZE": "4"})
        assert db["OPTIONS"]["pool"]["max_waiting"] == 8

    def test_pool_max_waiting_env_override(self):
        db = _prod_default_db({"PG_USE_POOL": "1", "PG_POOL_MAX_WAITING": "5"})
        assert db["OPTIONS"]["pool"]["max_waiting"] == 5

    def test_pool_malformed_max_size_falls_back_to_default(self):
        db = _prod_default_db({"PG_USE_POOL": "1", "PG_POOL_MAX_SIZE": "banana"})
        assert db["OPTIONS"]["pool"]["max_size"] == 16

    def test_conn_max_age_stays_zero_when_pooled(self):
        # Django refuses to pool with CONN_MAX_AGE != 0 (raises
        # ImproperlyConfigured on first DB access).
        db = _prod_default_db({"PG_USE_POOL": "1"})
        assert db["CONN_MAX_AGE"] == 0

    def test_conn_health_checks_enabled_when_pooled(self):
        # Django passes CONN_HEALTH_CHECKS through as psycopg_pool's
        # check-on-checkout callback; it is what transparently replaces
        # connections severed by failovers or server-side idle reapers.
        db = _prod_default_db({"PG_USE_POOL": "1"})
        assert db["CONN_HEALTH_CHECKS"] is True

    def test_connect_timeout_present_alongside_pool(self):
        db = _prod_default_db({"PG_USE_POOL": "1"})
        assert db["OPTIONS"]["connect_timeout"] == 10
