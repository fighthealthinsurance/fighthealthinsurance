import datetime
from typing import Optional

from django.core.mail import EmailMultiAlternatives
from django.db.models import QuerySet
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.urls import reverse
from django.views import View, generic

from fighthealthinsurance import common_view_logic
from fighthealthinsurance import forms as core_forms
from fighthealthinsurance.forms import FollowUpTestForm, ActivateProForm
from fighthealthinsurance.models import (
    Denial,
    FollowUpSched,
    InterestedProfessional,
    ProfessionalUser,
    UserDomain,
    ProfessionalDomainRelation,
)
from fighthealthinsurance.followup_emails import (
    ThankyouEmailSender,
    FollowUpEmailSender,
)


class ScheduleFollowUps(View):
    """A view to go through and schedule any missing follow ups."""

    def get(self, request):
        denials = (
            Denial.objects.filter(raw_email__isnull=False)
            .filter(followupsched__isnull=True)
            .iterator()
        )
        c = 0
        for denial in denials:
            # Shouldn't happen but makes the type checker happy.
            if denial.raw_email is None:
                continue
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
        field = form.cleaned_data.get("email")
        try:
            count = int(field)
            sent = s.send_all(count=field)
        except ValueError:
            sent = s.dosend(email=field)
        return HttpResponse(str(sent))


class ThankyouSenderView(generic.FormView):
    """A view to test the thankyou sender."""

    template_name = "followup_test.html"
    form_class = core_forms.FollowUpTestForm

    def form_valid(self, form):
        s = ThankyouEmailSender()
        field = form.cleaned_data.get("email")
        try:
            count = int(field)
            sent = s.send_all(count=field)
        except ValueError:
            sent = s.dosend(email=field)
        return HttpResponse(str(sent))


class ActivateProUserView(generic.FormView):
    template_name = "activate_pro.html"
    form_class = core_forms.ActivateProForm

    def form_valid(self, form):
        phonenumber = form.cleaned_data.get("phonenumber")
        domain = UserDomain.objects.get(visible_phone_number=phonenumber)
        domain.active = True
        domain.save()
        # Correctly update all professionals associated with the domain.
        ProfessionalUser.objects.filter(domains__in=[domain]).update(active=True)
        for p in ProfessionalUser.objects.filter(domains__in=[domain]):
            user = p.user
            user.is_active = True
            user.save()
        return HttpResponse("Pro user activated")


class FollowUpFaxSenderView(generic.FormView):
    """A view to test the follow up sender."""

    template_name = "followup_test.html"
    form_class = core_forms.FollowUpTestForm

    def form_valid(self, form):
        field = form.cleaned_data.get("email")
        helper = common_view_logic.SendFaxHelper

        if field.isdigit():
            sent = helper.blocking_dosend_all(count=field)
        else:
            sent = helper.blocking_dosend_target(email=field)

        return HttpResponse(str(sent))
