import asyncio
import os
import time
import uuid
from datetime import timedelta
from typing import Tuple, Union

from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

import ray

from fighthealthinsurance.fax_utils import *
from fighthealthinsurance.utils import get_env_variable

# Use stdlib logging (not loguru) in Ray actors: loguru's logger object
# cannot be serialized by Ray's cloudpickle, causing actor init failures.
# stdlib logging is routed to loguru via dj_easy_log's InterceptHandler.
import logging

logger = logging.getLogger(__name__)


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
            logger.warning(f"Couldn't delete faxes: {e}")
            call_command("migrate")

    def send_delayed_faxes(self) -> Tuple[int, int]:
        from fighthealthinsurance.models import FaxesToSend

        target_time = timezone.now() - timedelta(hours=1)
        logger.info(f"Sending faxes older than target: {target_time}")

        delayed_faxes = FaxesToSend.objects.filter(
            should_send=True,
            sent=False,
            date__lt=target_time,
        )
        t = 0
        f = 0
        for fax in delayed_faxes:
            try:
                logger.debug(f"Attempting to send fax {fax}")
                t = t + 1
                response = self.do_send_fax_object(fax)
                if response:
                    logger.info(f"Sent fax {fax} successfully")
                else:
                    logger.warning(f"Failed to send fax {fax}")
                    f = f + 1
            except Exception as e:
                logger.error(f"Error sending fax {fax}", exc_info=True)
                f = f + 1
        if t > 0:
            logger.info(f"Tried sending {t} faxes with {f} failures")
        else:
            logger.debug("No old faxes found to send")
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
            logger.warning(f"Fax not found for uuid={uuid_val}")
            return False
        return self.do_send_fax_object(fax)

    def _update_fax_for_sending(self, fax):
        logger.debug("Recording attempt to send time")
        fax.attempting_to_send_as_of = timezone.now()
        fax.save()

    def _update_fax_for_sent(self, fax, fax_success, missing_destination):
        logger.debug("Fax send command returned")
        email = fax.email
        fax.sent = True
        fax.fax_success = fax_success
        fax.save()
        logger.debug(f"Checking if we should notify user of result {fax_success}")
        if fax.professional:
            logger.debug("Professional fax, updating appeal")
            appeal = fax.for_appeal
            if appeal is not None:
                appeal.sent = fax_success
                appeal.save()
                return True
            else:
                logger.warning(f"No appeal found for professional fax {fax}")
                return True
        fax_redo_link = "https://www.fighthealthinsurance.com" + reverse(
            "fax-followup",
            kwargs={
                "hashed_email": fax.hashed_email,
                "uuid": fax.uuid,
            },
        )
        context = {
            "name": fax.name,
            "success": fax_success,
            "fax_redo_link": fax_redo_link,
            "missing_destination": missing_destination,
        }
        # First, render the plain text content.
        text_content = render_to_string(
            "emails/fax_followup.txt",
            context=context,
        )

        # Secondly, render the HTML content.
        html_content = render_to_string(
            "emails/fax_followup.html",
            context=context,
        )
        # Then, create a multipart email instance.
        msg = EmailMultiAlternatives(
            "Following up from Fight Health Insurance Fax Service",
            text_content,
            "support42@fighthealthinsurance.com",
            [email],
        )
        msg.attach_alternative(html_content, "text/html")
        msg.send()
        logger.info("Fax follow-up email sent")

    def do_send_fax_object(self, fax) -> bool:
        denial = fax.denial_id
        if denial is None:
            logger.warning(f"Fax {fax} has no denial id")
            return False
        if fax.destination is None:
            logger.warning(f"Fax {fax} has no destination")
            self._update_fax_for_sent(fax, False, missing_destination=True)
            return False
        extra = ""
        if denial.claim_id is not None and len(denial.claim_id) > 2:
            extra += f"This is regarding claim id {denial.claim_id}."
        if fax.name is not None and len(fax.name) > 2:
            extra += f"This fax is sent on behalf of {fax.name}."
        self._update_fax_for_sending(fax)
        logger.debug("Kicking off fax sending")
        fax_sent = False
        try:
            fax_sent = asyncio.run(
                flexible_fax_magic.send_fax(
                    input_paths=[fax.get_temporary_document_path()],
                    extra=extra,
                    destination=fax.destination,
                    blocking=True,
                    professional=fax.professional,
                )
            )
        except Exception as e:
            logger.error("Error running async send_fax", exc_info=True)
        self._update_fax_for_sent(fax, fax_sent, missing_destination=False)
        return fax_sent
