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

        # Soft-fail visibility for IP geo lookups (chat state guessing +
        # ASN tracking): warn once, naming FHI_GEOIP_CITY_DB, when they are
        # disabled — otherwise the features silently return nothing.
        from fhi_users.audit import warn_if_geo_lookups_disabled

        warn_if_geo_lookups_disabled()
