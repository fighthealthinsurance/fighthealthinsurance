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
