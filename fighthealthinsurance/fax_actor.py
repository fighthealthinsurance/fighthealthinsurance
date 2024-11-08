from typing import Tuple
import ray
from fighthealthinsurance.fax_utils import *
from django.utils import timezone
from datetime import datetime, timedelta
import time


@ray.remote(max_restarts=-1, max_task_retries=-1)
class FaxActor:
    def __init__(self):
        time.sleep(1)
        # This is a bit of a hack but we do this so we have the app configured
        from configurations.wsgi import get_wsgi_application

        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "fighthealthinsurance.settings")
        get_wsgi_application()
        from fighthealthinsurance.models import FaxesToSend

    def hi(self):
        return "ok"

    def db_settings(self):
        from django.db import connection

        return str(dict(connection.settings_dict))

    def version(self):
        """Bump this to restart the fax actor."""
        return 1

    def send_delayed_faxes(self) -> Tuple[int, int]:
        from fighthealthinsurance.models import FaxesToSend

        target_time = timezone.now() - timedelta(hours=1)
        print(f"Sending faxes older than target: {target_time}")

        delayed_faxes = FaxesToSend.objects.filter(
            should_send=True, sent=False, date__lt=target_time
        )
        t = 0
        f = 0
        for fax in delayed_faxes:
            try:
                print(f"Attempting to send fax {fax}")
                t = t + 1
                self.do_send_fax_object(fax)
                print(f"Sent fax {fax}")
            except Exception as e:
                print(f"Error sending fax {fax}: {e}")
                f = f + 1
        return (t, f)

    def do_send_fax(self, hashed_email: str, uuid: str) -> bool:
        # Now that we have an app instance we can import faxes to send
        from fighthealthinsurance.models import FaxesToSend

        fax = FaxesToSend.objects.filter(uuid=uuid, hashed_email=hashed_email).get()
        return self.do_send_fax_object(fax)

    def do_send_fax_object(self, fax) -> bool:
        email = fax.email
        denial = fax.denial_id
        if denial is None:
            return False
        if fax.destination is None:
            return False
        extra = ""
        if denial.claim_id is not None and len(denial.claim_id) > 2:
            extra += f"This is regarding claim id {denial.claim_id}."
        if fax.name is not None and len(fax.name) > 2:
            extra += f"This fax is sent on behalf of {fax.name}."
        print(f"Recording attempt to send time")
        fax.attempting_to_send_as_of = timezone.now()
        fax.save()
        print(f"Kicking of fax sending")
        fax_sent = flexible_fax_magic.send_fax(
            input_paths=[fax.get_temporary_document_path()],
            extra=extra,
            destination=fax.destination,
            blocking=True,
        )
        print(f"Fax send command returned :)")
        fax.sent = True
        fax.fax_success = fax_sent
        fax.save()
        print(f"Notifing user of result {fax.fax_success}")
        fax_redo_link = "https://www.fighthealthinsurance.com" + reverse(
            "fax-followup",
            kwargs={
                "hashed_email": fax.hashed_email,
                "uuid": fax.uuid,
            },
        )
        context = {
            "name": fax.name,
            "success": fax_sent,
            "fax_redo_link": fax_redo_link,
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
            "Following up from Fight Health Insurance",
            text_content,
            "support42@fighthealthinsurance.com",
            [email],
        )
        msg.attach_alternative(html_content, "text/html")
        msg.send()
        print(f"E-mail sent!")
        return True
