from django.urls import reverse
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.decorators import method_decorator
from django.contrib.admin.views.decorators import staff_member_required
from django_celery_beat.models import PeriodicTasks
from django.views import View, generic
from django.http import HttpResponse
from django.db.models import QuerySet
from fighthealthinsurance.models import Denial, FollowUpSched
from fighthealthinsurance.core_forms import FollowUpTestForm
import datetime
from typing import Optional


class ScheduleFollowUps(View):
    """A view to go through and schedule any missing follow ups."""

    @method_decorator(staff_member_required)
    def get(self, request):
        denials = (
            Denial.objects.filter(raw_email__isnull=False)
            .filter(followupsched__isnull=True)
            .iterator()
        )
        c = 0
        for denial in denials:
            FollowUpSched.objects.create(
                email=denial.raw_email,
                follow_up_date=denial.date + datetime.timedelta(days=15),
                denial_id=denial,
            )
            c = c + 1
        return HttpResponse(str(c))


class FollowUpEmailSenderView(generic.FormView):
    """A view to test the follow up sender."""

    template_name = "followup_test.html"
    form_class = FollowUpTestForm

    def form_valid(self, form):
        s = FollowUpEmailSender()
        sent = s.dosend(email=form.cleaned_data.get("email"))
        return HttpResponse(str(sent))


class FollowUpEmailSender:
    def find_candidates(self) -> QuerySet[FollowUpSched, FollowUpSched]:
        candidates = FollowUpSched.objects.filter(follow_up_sent=False).filter(
            follow_up_date__lt=datetime.date.today()
        )
        return candidates

    def send_all(self) -> int:
        candidates = self.find_candidates()
        return len(list(map(lambda f: self.dosend(follow_up_sched=f), candidates)))

    def dosend(
        self,
        follow_up_sched: Optional[FollowUpSched] = None,
        email: Optional[str] = None,
    ) -> bool:
        if follow_up_sched is None and email is not None:
            follow_up_sched = (
                FollowUpSched.objects.filter(email=email)
                .filter(follow_up_sent=False)
                .get()
            )
        elif email is None and follow_up_sched is not None:
            email = follow_up_sched.email
        else:
            raise Exception("One of email and follow_up_sched must be set.")
        denial = follow_up_sched.denial_id
        selected_appeal = denial.chose_appeal()
        context = {
            "selected_appeal": selected_appeal,
            "followup_link": request.build_absolute_uri(
                reverse(
                    "followup",
                    kwargs={
                        "uuid": denial.uuid,
                        "hashed_email": denial.hashed_email,
                        "follow_up_semi_sekret": denial.follow_up_semi_sekret,
                    },
                )),
        }

        # First, render the plain text content.
        text_content = render_to_string(
            "emails/followup.txt",
            context=context,
        )

        # Secondly, render the HTML content.
        html_content = render_to_string(
            "emails/followup.html",
            context=context,
        )

        # Then, create a multipart email instance.
        msg = EmailMultiAlternatives(
            "Following up from Fight Health Insurance",
            text_content,
            "support42@fighthealthinsurance.com",
            [email],
        )

        # Lastly, attach the HTML content to the email instance and send.
        msg.attach_alternative(html_content, "text/html")
        try:
            msg.send()
            follow_up_sched.follow_up_sent = True
            follow_up_sched.save()
            return True
        except:
            return False
