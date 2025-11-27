import datetime
import smtplib

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from django.http import HttpResponse
from django.views import View, generic
from django.db import transaction
from loguru import logger

from fighthealthinsurance import common_view_logic
from fighthealthinsurance import forms as core_forms
from fighthealthinsurance.forms import FollowUpTestForm
from fighthealthinsurance.models import (
    Denial,
    FollowUpSched,
    MailingListSubscriber,
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
    template_name = "pro_domain_task.html"
    form_class = core_forms.ActivateProForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = "Activate Pro User"
        context["heading"] = "Activate Pro User Domain"
        context["description"] = "Enter the phone number of the domain to activate."
        context["button_text"] = "Activate"
        return context

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
        for r in ProfessionalDomainRelation.objects.filter(domain=domain):
            r.active_domain_relation = True
            r.pending_domain_relation = False
            r.save()
        return HttpResponse("Pro user activated")


class EnableBetaForDomainView(generic.FormView):
    """A view to enable beta features for a user domain by phone number."""

    template_name = "pro_domain_task.html"
    form_class = core_forms.ActivateProForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = "Enable Beta Features"
        context["heading"] = "Enable Beta Features for Domain"
        context["description"] = (
            "Enter the phone number of the domain to enable beta features."
        )
        context["button_text"] = "Enable Beta"
        return context

    def form_valid(self, form):
        try:
            phonenumber = form.cleaned_data.get("phonenumber")
            domain = UserDomain.objects.get(visible_phone_number=phonenumber)
            with transaction.atomic():
                domain.beta = True
                domain.save()
            return HttpResponse(
                f"Beta features enabled for domain {domain.name} ({phonenumber})"
            )
        except UserDomain.DoesNotExist:
            return HttpResponse(
                f"No domain found with phone number {phonenumber}", status=404
            )
        except Exception as e:
            logger.opt(exception=True).error(
                f"Error enabling beta for domain with phone {phonenumber}: {str(e)}"
            )
            return HttpResponse(f"Error enabling beta: {str(e)}", status=500)


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


class SendMailingListMailView(generic.FormView):
    """A view to send emails to all mailing list subscribers."""

    template_name = "send_mailing_list_mail.html"
    form_class = core_forms.SendMailingListMailForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["subscriber_count"] = MailingListSubscriber.objects.count()
        return context

    def form_valid(self, form):
        subject = form.cleaned_data.get("subject")
        html_content = form.cleaned_data.get("html_content")
        text_content = form.cleaned_data.get("text_content")
        test_email = form.cleaned_data.get("test_email")

        sent_count = 0
        failed_count = 0

        if test_email:
            # Send only to test email
            recipients = [test_email]
        else:
            # Use iterator to avoid loading all emails into memory at once
            recipients = MailingListSubscriber.objects.values_list(
                "email", flat=True
            ).iterator()

        # Use connection reuse for better performance
        connection = get_connection()
        try:
            connection.open()
            for email in recipients:
                try:
                    msg = EmailMultiAlternatives(
                        subject,
                        text_content,
                        settings.DEFAULT_FROM_EMAIL,
                        to=[email],
                        connection=connection,
                    )
                    msg.attach_alternative(html_content, "text/html")
                    msg.send()
                    sent_count += 1
                    logger.info(f"Sent mailing list email to {email}")
                except smtplib.SMTPException as e:
                    failed_count += 1
                    logger.error(f"SMTP error sending mailing list email to {email}: {e}")
                except OSError as e:
                    failed_count += 1
                    logger.error(f"Connection error sending mailing list email to {email}: {e}")
        finally:
            connection.close()

        if test_email:
            return HttpResponse(
                f"Test email sent successfully to {test_email}"
            )
        else:
            return HttpResponse(
                f"Mailing list email sent. Success: {sent_count}, Failed: {failed_count}"
            )
