import datetime
from typing import Optional

from django.contrib.admin.views.decorators import staff_member_required
from django.core.mail import EmailMultiAlternatives
from django.db.models import QuerySet
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View, generic

from fighthealthinsurance.forms import FollowUpTestForm
from fighthealthinsurance.models import Denial, FollowUpSched, InterestedProfessional
from fighthealthinsurance.utils import send_fallback_email


class ThankyouEmailSender(object):
    def find_candidates(
        self,
    ) -> QuerySet[InterestedProfessional, InterestedProfessional]:
        candidates = InterestedProfessional.objects.filter(thankyou_email_sent=False)
        return candidates

    def send_all(self, count: Optional[int] = None) -> int:
        candidates = self.find_candidates()
        selected_candidates = candidates
        if count is not None:
            selected_candidates = candidates[:count]
        return len(
            list(map(lambda f: self.dosend(interested_pro=f), selected_candidates))
        )

    def dosend(
        self,
        email: Optional[str] = None,
        interested_pro: Optional[InterestedProfessional] = None,
    ) -> bool:
        if email is not None:
            interested_pro = InterestedProfessional.objects.filter(email=email)[0]
        if interested_pro is None:
            return False
        email = interested_pro.email
        context = {
            "name": interested_pro.name,
        }

        try:
            send_fallback_email(
                template_name="professional_thankyou",
                subject="Thank you for signing up for Fight Health Insurance Pro Beta!",
                context=context,
                to_email=email,
            )
            interested_pro.thankyou_email_sent = True
            interested_pro.save()
            return True
        except:
            return False


class FollowUpEmailSender(object):
    def find_candidates(self) -> QuerySet[FollowUpSched, FollowUpSched]:
        candidates = FollowUpSched.objects.filter(follow_up_sent=False).filter(
            follow_up_date__lt=datetime.date.today()
        )
        return candidates

    def send_all(self, count: Optional[int] = None) -> int:
        candidates = self.find_candidates()
        selected_candidates = candidates
        if count is not None:
            selected_candidates = candidates[:count]
        return len(
            list(map(lambda f: self.dosend(follow_up_sched=f), selected_candidates))
        )

    def dosend(
        self,
        follow_up_sched: Optional[FollowUpSched] = None,
        email: Optional[str] = None,
    ) -> bool:
        if follow_up_sched is None and email is not None:
            follow_up_sched = FollowUpSched.objects.filter(email=email).filter(
                follow_up_sent=False
            )[0]
        elif email is None and follow_up_sched is not None:
            email = follow_up_sched.email
        else:
            raise Exception("One of email and follow_up_sched must be set.")
        denial = follow_up_sched.denial_id
        selected_appeal = denial.chose_appeal()
        context = {
            "selected_appeal": selected_appeal,
            "followup_link": "https://www.fighthealthinsurance.com"
            + reverse(
                "followup",
                kwargs={
                    "uuid": denial.uuid,
                    "hashed_email": denial.hashed_email,
                    "follow_up_semi_sekret": denial.follow_up_semi_sekret,
                },
            ),
        }

        try:
            send_fallback_email(
                template_name="followup",
                subject="Following up from Fight Health Insurance",
                context=context,
                to_email=email,
            )
            follow_up_sched.follow_up_sent = True
            follow_up_sched.save()
            return True
        except:
            return False
