"""Operator-facing model-backend health check.

Deployment usage (from start-server.sh's POLLING_ACTORS job, once per deploy)::

    python manage.py check_model_backends --deploy-hook

Manual usage (any pod / local shell with the right env)::

    python manage.py check_model_backends                    # all enabled
    python manage.py check_model_backends --model anthropic/claude-sonnet-4-6
    python manage.py check_model_backends --model claude-sonnet-4-6 --timeout 15

Exit codes: manual runs exit 1 when any requested check fails (usable in
scripts/CI); ``--deploy-hook`` exits 0 on failures unless strict mode
(``FHI_MODEL_HEALTH_STRICT=1``) is enabled, so an unhealthy provider degrades
gracefully instead of rolling back the deploy.
"""

import os
from typing import Any

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Check every enabled model backend end-to-end with a tiny prompt and "
        "report categorized results (auth, model-not-found, rate-limit, "
        "timeout, ...). With --deploy-hook, runs once per deployment via a "
        "database leader claim and emails a single consolidated failure "
        "report to support."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--model",
            action="append",
            dest="models",
            default=None,
            metavar="NAME",
            help=(
                "Check only this model (friendly name like "
                "'anthropic/claude-sonnet-4-6' or internal name like "
                "'claude-sonnet-4-6'). Repeatable."
            ),
        )
        parser.add_argument(
            "--timeout",
            type=float,
            default=None,
            help="Per-backend timeout in seconds (default 30, or "
            "FHI_MODEL_HEALTH_TIMEOUT).",
        )
        parser.add_argument(
            "--deploy-hook",
            action="store_true",
            help=(
                "Deployment mode: claim the once-per-deployment leader slot, "
                "persist results, send the consolidated alert email on "
                "failures, and exit 0 unless FHI_MODEL_HEALTH_STRICT=1."
            ),
        )
        parser.add_argument(
            "--no-persist",
            action="store_true",
            help="Do not write results to the ModelBackendHealthCheckResult table.",
        )

    def handle(self, *args: str, **options: Any):
        from fighthealthinsurance.ml import model_health_check as mhc

        deploy_hook: bool = options["deploy_hook"]
        timeout = options["timeout"]
        if timeout is None:
            try:
                timeout = float(
                    os.getenv("FHI_MODEL_HEALTH_TIMEOUT", "")
                    or mhc.DEFAULT_TIMEOUT_SECONDS
                )
            except ValueError:
                timeout = mhc.DEFAULT_TIMEOUT_SECONDS

        summary = mhc.run_health_check(
            only_models=options["models"],
            timeout=timeout,
            require_leader=deploy_hook,
            send_alert_email=deploy_hook,
            persist=not options["no_persist"],
        )

        if not summary.ran_checks:
            if deploy_hook:
                self.stdout.write(
                    "Model backend health check skipped (another process "
                    "already ran it for this deployment, or it could not run)."
                )
                # A lost leader claim is normal; only strict mode + an actual
                # inability to verify should fail the deploy, and we can't
                # tell those apart here without racing the winner — so always
                # exit 0 on skip.
                return
            self.stderr.write(
                self.style.ERROR("Model backend health check could not run.")
            )
            raise SystemExit(1)

        self.stdout.write(mhc.format_summary_block(summary))
        if options["models"] and not summary.results:
            self.stderr.write(
                self.style.ERROR(
                    f"No configured model matched {options['models']!r}. "
                    "Use the friendly registry name (see the "
                    "MODEL_BACKEND_HEALTH_SUMMARY of a full run) or the "
                    "internal/wire name."
                )
            )
            raise SystemExit(1)

        failures = summary.failures
        if failures:
            self.stdout.write(
                self.style.WARNING(
                    f"{len(failures)}/{len(summary.results)} backend check(s) "
                    "failed."
                )
            )
            if summary.email_sent:
                self.stdout.write("Consolidated failure alert emailed to support.")
            if deploy_hook:
                if mhc.strict_mode_enabled():
                    self.stderr.write(
                        self.style.ERROR(
                            "FHI_MODEL_HEALTH_STRICT=1: failing the deploy hook."
                        )
                    )
                    raise SystemExit(2)
                self.stdout.write(
                    "Non-strict mode: healthy backends remain available; not "
                    "failing the deployment."
                )
                return
            raise SystemExit(1)

        self.stdout.write(
            self.style.SUCCESS(
                f"All {len(summary.results)} checked backend(s) healthy."
            )
        )
