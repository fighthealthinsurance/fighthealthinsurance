from typing import Tuple, Union
import ray
from fighthealthinsurance.fax_utils import *
import uuid
from datetime import timedelta
import time
import asyncio

from django.template.loader import render_to_string
from django.core.mail import EmailMultiAlternatives
from django.utils import timezone
from django.urls import reverse
import logging

logger = logging.getLogger(__name__)
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
        from fighthealthinsurance.models import FaxesToSend
        from django.core.management import call_command

        self._require_test_env()

        try:
            FaxesToSend.objects.all().delete()
        except Exception as e:
            print(f"Couldn't delete faxes {e}")
            call_command("migrate")

    def send_delayed_faxes(self) -> Tuple[int, int]:
        from fighthealthinsurance.models import FaxesToSend

        target_time = timezone.now() - timedelta(hours=1)
        print(f"Sending faxes older than target: {target_time}")

        delayed_faxes = FaxesToSend.objects.filter(
            should_send=True,
            sent=False,
            date__lt=target_time,
            destination__isnull=False,
        )
        t = 0
        f = 0
        for fax in delayed_faxes:
            try:
                print(f"Attempting to send fax {fax}")
                t = t + 1
                response = self.do_send_fax_object(fax)
                print(f"Sent fax {fax} with result {response}")
            except Exception as e:
                print(f"Error sending fax {fax}: {e}")
                f = f + 1
        return (t, f)

    def do_send_fax(self, hashed_email: str, uuid_val: Union[str, uuid.UUID]) -> bool:
        # Now that we have an app instance we can import faxes to send
        from fighthealthinsurance.models import FaxesToSend

        # Convert UUID object to string if needed
        if not isinstance(uuid_val, str):
            uuid_val = str(uuid_val)
        fax = FaxesToSend.objects.filter(uuid=uuid_val, hashed_email=hashed_email).get()
        return self.do_send_fax_object(fax)

    def _update_fax_for_sending(self, fax):
        logger.debug("Recording attempt to send time")
        fax.attempting_to_send_as_of = timezone.now()
        fax.save()

    def _update_fax_for_sent(self, fax, fax_success):
        logger.debug("Fax send command returned")
        email = fax.email
        # Mark fax as sent and record success/failure
        fax.sent = True
        fax.fax_success = fax_success
        fax.save()
        logger.debug("Should notify user of result: %s", fax_success)
        logger.debug("fax.professional: %s", fax.professional)
        # For professional faxes, we don't send email notifications to users
        # Instead, we just update the appeal status and return early
        if fax.professional:
            logger.info("Professional fax, no need to notify user -- updating appeal")
            # for_appeal is a ForeignKey to an Appeal instance
            appeal = fax.for_appeal
            if appeal:
                appeal.sent = fax_success
                appeal.save()
            return True

        # Email notification logic only executes for non-professional faxes
        # The following code builds and sends a notification email to the user
        logger.debug("About to call reverse for fax-followup with hashed_email=%s, uuid=%s", fax.hashed_email, fax.uuid)
        try:
            fax_redo_link = "https://www.fighthealthinsurance.com" + reverse(
                "fax-followup",
                kwargs={
                    "hashed_email": fax.hashed_email,
                    "uuid": fax.uuid,
                },
            )
            print(f"reverse() returned: {fax_redo_link}")
        except Exception as e:
            logger.warning("Exception in reverse(): %s", e)
            fax_redo_link = "https://www.fighthealthinsurance.com/mock-url-path"

        context = {
            "name": fax.name,
            "success": fax_success,
            "fax_redo_link": fax_redo_link,
        }
        logger.debug("Email context: %s", context)
        # Render the plain text content
        try:
            text_content = render_to_string(
                "emails/fax_followup.txt",
                context=context,
            )
            logger.debug("Rendered text_content: %s", text_content[:100])
        except Exception as e:
            logger.error("Exception rendering text template: %s", e)
            text_content = "[template error]"

        # Render the HTML content
        try:
            html_content = render_to_string(
                "emails/fax_followup.html",
                context=context,
            )
            logger.debug("Rendered html_content: %s", html_content[:100])
        except Exception as e:
            logger.error("Exception rendering html template: %s", e)
            html_content = "[template error]"

        # Create and send the multipart email
        try:
            msg = EmailMultiAlternatives(
                "Following up from Fight Health Insurance",
                text_content,
                "support42@fighthealthinsurance.com",
                [email],
            )
            msg.attach_alternative(html_content, "text/html")
            logger.info("About to send email to: %s", email)
            msg.send()
            logger.info("Email sent to %s", email)
        except Exception as e:
            logger.error("Exception sending email: %s", e)

    def do_send_fax_object(self, fax) -> bool:
        denial = fax.denial_id
        if denial is None:
            print(f"Fax {fax} has no denial id")
            return False
        if fax.destination is None:
            print(f"Fax {fax} has no destination")
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
            logger.error("Error running async send_fax: %s", e)
        self._update_fax_for_sent(fax, fax_sent)
        return True
