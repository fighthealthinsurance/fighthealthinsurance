import time
import uuid
from datetime import timedelta
from typing import Tuple, Union

from django.utils import timezone

import ray

from fighthealthinsurance import fax_send_core
from fighthealthinsurance.fax_utils import *
from fighthealthinsurance.utils import get_env_variable


@ray.remote(max_restarts=-1, max_task_retries=-1)
class FaxActor:
    def __init__(self):
        time.sleep(1)
        # This is a bit of a hack but we do this so we have the app configured
        from configurations.wsgi import get_wsgi_application

        os.environ.setdefault(
            "DJANGO_SETTINGS_MODULE",
            get_env_variable("DJANGO_SETTINGS_MODULE", "fighthealthinsurance.settings"),
        )
        get_wsgi_application()
        from loguru import logger

        self._logger = logger

    def hi(self):
        return "ok"

    def db_settings(self):
        from django.db import connection

        return str(dict(connection.settings_dict))

    def version(self):
        """Bump this to restart the fax actor."""
        return 1

    def _require_test_env(self):
        """Call this at the start of test only functions."""
        env = os.getenv("DJANGO_CONFIGURATION")
        if env not in ["Test", "TestActor", "TestSync"]:
            raise Exception(f"Tried to call test migrate in non-test env -- {env}")

    def test_create_fax_object(self, **kwargs):
        from fighthealthinsurance.models import FaxesToSend

        self._require_test_env()

        fax = FaxesToSend.objects.create(**kwargs)
        # reset the date to the specified old date for testing.
        if "date" in kwargs:
            fax.date = kwargs["date"]
            fax.save()
        return fax

    def test_delete(self, fax):
        self._require_test_env()
        return fax.delete()

    def test_migrate(self):
        from django.core.management import call_command

        from fighthealthinsurance.models import FaxesToSend

        self._require_test_env()

        try:
            FaxesToSend.objects.all().delete()
        except Exception as e:
            self._logger.warning(f"Couldn't delete faxes: {e}")
            call_command("migrate")

    def send_delayed_faxes(self) -> Tuple[int, int]:
        from django.conf import settings

        from fighthealthinsurance.models import FaxesToSend

        if getattr(settings, "TEMPORAL_ENABLED", False):
            # Temporal's SendFaxWorkflow (delay_send timer) owns delayed sending;
            # the polling actor stays idle to avoid duplicate work.
            self._logger.debug(
                "TEMPORAL_ENABLED: skipping Ray delayed-fax sweep (Temporal owns it)"
            )
            return (0, 0)

        target_time = timezone.now() - timedelta(hours=1)
        self._logger.info(f"Sending faxes older than target: {target_time}")

        delayed_faxes = FaxesToSend.objects.filter(
            should_send=True,
            sent=False,
            date__lt=target_time,
        )
        t = 0
        f = 0
        for fax in delayed_faxes:
            try:
                self._logger.debug(f"Attempting to send fax {fax}")
                t = t + 1
                response = self.do_send_fax_object(fax)
                if response:
                    self._logger.info(f"Sent fax {fax} successfully")
                else:
                    self._logger.warning(f"Failed to send fax {fax}")
                    f = f + 1
            except Exception:
                self._logger.opt(exception=True).error(f"Error sending fax {fax}")
                f = f + 1
        if t > 0:
            self._logger.info(f"Tried sending {t} faxes with {f} failures")
        else:
            self._logger.debug("No old faxes found to send")
        return (t, f)

    def do_send_fax(self, hashed_email: str, uuid_val: Union[str, uuid.UUID]) -> bool:
        # Now that we have an app instance we can import faxes to send
        from fighthealthinsurance.models import FaxesToSend

        # Convert UUID object to string if needed
        if not isinstance(uuid_val, str):
            uuid_val = str(uuid_val)
        try:
            fax = FaxesToSend.objects.filter(
                uuid=uuid_val, hashed_email=hashed_email
            ).get()
        except FaxesToSend.DoesNotExist:
            self._logger.warning(f"Fax not found for uuid={uuid_val}")
            return False
        return self.do_send_fax_object(fax)

    def do_send_fax_object(self, fax) -> bool:
        # The precheck -> send -> finalize sequence lives in fax_send_core so the
        # Temporal SendFaxWorkflow can run the exact same steps as separate,
        # individually-retryable activities.
        return fax_send_core.do_send_fax_object(fax)
