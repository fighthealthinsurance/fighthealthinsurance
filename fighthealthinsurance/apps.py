from django.apps import AppConfig


class FightHealthInsuranceConfig(AppConfig):
    name = "fighthealthinsurance"

    def ready(self) -> None:
        # Export psycopg connection-pool stats on the django_prometheus
        # /metrics endpoint. Emits sample-less families unless pooling is
        # enabled (Prod's PG_USE_POOL), so it is safe in every
        # configuration.
        from fighthealthinsurance.db_pool_metrics import register_pool_stats_collector

        register_pool_stats_collector()

        # Intake outbox backlog gauges on the same endpoint (zero-cost while
        # nothing is pending; degrades to a log line if the table is not
        # migrated yet).
        from fighthealthinsurance.intake_outbox_metrics import (
            register_intake_outbox_collector,
        )

        register_intake_outbox_collector()

        # Soft-fail visibility for IP geo lookups (chat state guessing +
        # ASN tracking): warn once, naming FHI_GEOIP_CITY_DB, when they are
        # disabled — otherwise the features silently return nothing.
        from fhi_users.audit import (
            warm_geo_reader_in_background,
            warn_if_geo_lookups_disabled,
        )

        warn_if_geo_lookups_disabled()
        # Parse the (multi-second) city database at startup in a daemon
        # thread, so no request — including the synchronous ASN lookup that
        # runs inside the async chat consumer — pays the load on the event
        # loop.
        warm_geo_reader_in_background()
