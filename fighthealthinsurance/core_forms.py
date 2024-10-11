import os

from django import forms

from django_recaptcha.fields import ReCaptchaField, ReCaptchaV2Checkbox, ReCaptchaV3

from bs4 import BeautifulSoup

import pymupdf
import re
import urllib
import requests

from fighthealthinsurance.form_utils import *
from fighthealthinsurance.models import (
    Denial,
    DenialTypes,
    PlanSource,
    PlanType,
    InterestedProfessional,
)


# Actual forms
class InterestedProfessionalForm(forms.ModelForm):
    business_name = forms.CharField(required=False)
    address = forms.CharField(
        required=False, widget=forms.Textarea(attrs={"cols": 80, "rows": 5})
    )
    comments = forms.CharField(
        required=False, widget=forms.Textarea(attrs={"cols": 80, "rows": 5})
    )
    phone_number = forms.CharField(required=False)
    job_title_or_provider_type = forms.CharField(required=False)
    most_common_denial = forms.CharField(required=False)
    clicked_for_paid = forms.BooleanField(
        initial=True,
        required=False,
        label="Optional: Pay $10 now to get 3-months of the beta when we launch the professional version while we figure out what/if folks will pay for it.",
    )

    class Meta:
        model = InterestedProfessional
        exclude = ["paid", "signup_date"]


class DeleteDataForm(forms.Form):
    email = forms.CharField(required=True)


class ShareAppealForm(forms.Form):
    denial_id = forms.IntegerField(required=True, widget=forms.HiddenInput())
    email = forms.CharField(required=True, widget=forms.HiddenInput())
    appeal_text = forms.CharField(required=True)


class ChooseAppealForm(forms.Form):
    denial_id = forms.IntegerField(required=True, widget=forms.HiddenInput())
    email = forms.CharField(required=True, widget=forms.HiddenInput())
    appeal_text = forms.CharField(required=True)


class DenialForm(forms.Form):
    zip = forms.CharField(required=False)
    pii = forms.BooleanField(required=True)
    tos = forms.BooleanField(required=True)
    privacy = forms.BooleanField(required=True)
    store_raw_email = forms.BooleanField(required=False)
    use_external_models = forms.BooleanField(required=False)
    denial_text = forms.CharField(required=True)
    health_history = forms.CharField(required=False)
    email = forms.EmailField(required=True)
    plan_documents = MultipleFileField(required=False)


class DenialRefForm(forms.Form):
    denial_id = forms.IntegerField(required=True, widget=forms.HiddenInput())
    email = forms.CharField(required=True, widget=forms.HiddenInput())
    semi_sekret = forms.CharField(required=True, widget=forms.HiddenInput())


class PostInferedForm(DenialRefForm):
    """The form to double check what we infered. This leads to our next steps /
    FindNextSteps."""

    # Send denial id and e-mail back that way people can't just change the ID
    # and get someone elses denial.
    denial_id = forms.IntegerField(required=True, widget=forms.HiddenInput())
    email = forms.CharField(required=True, widget=forms.HiddenInput())
    denial_type = forms.ModelMultipleChoiceField(
        queryset=DenialTypes.objects.all(), required=False
    )
    denial_type_text = forms.CharField(
        required=False,
        label="Denial Type (text, you can type if the categories don't match the denial type)",
    )
    plan_id = forms.CharField(required=False)
    claim_id = forms.CharField(required=False)
    insurance_company = forms.CharField(required=False)
    plan_source = forms.ModelMultipleChoiceField(
        queryset=PlanSource.objects.all(), required=False
    )
    #    plan_type = forms.ModelMultipleChoiceField(queryset=PlanType.objects.all())
    #    plan_type_text = forms.CharField(required=False)
    employer_name = forms.CharField(required=False)
    denial_date = forms.DateField(required=False)
    your_state = forms.CharField(max_length=2, required=False)
    procedure = forms.CharField(
        max_length=200,
        label="What is the procedure/treatment you had denied?",
        required=False,
    )
    diagnosis = forms.CharField(
        max_length=200,
        label="What is the diagnosis (if any) associated with the request."
        + "Does not need to be a disease it can be any number of personal factors, "
        + 'including things like "high risk homosexual behaviour" (yeah that\'s a real one)',
        required=False,
    )

    captcha = forms.CharField(required=False, widget=forms.HiddenInput())
    # Instead of the default behaviour we skip the recaptcha field entirely for dev.
    if "RECAPTCHA_PUBLIC_KEY" in os.environ and (
        "RECAPTCHA_TESTING" not in os.environ
        or os.environ["RECAPTCHA_TESTING"].lower() != "true"
    ):
        captcha = ReCaptchaField(widget=ReCaptchaV2Checkbox())


class FollowUpForm(forms.Form):
    Appeal_Result_Choices = [
        ("Do not wish to disclose", "Do not wish to disclose"),
        ("No Appeal Sent", "No Appeal Sent"),
        ("Yes", "Yes"),
        ("Partial", "Partial"),
        ("No", "No"),
        ("Do not know yet", "Do not know yet"),
        ("Other", "Other -- see comments"),
    ]

    uuid = forms.UUIDField(required=True, widget=forms.HiddenInput)
    follow_up_semi_sekret = forms.CharField(required=True, widget=forms.HiddenInput)
    hashed_email = forms.CharField(required=True, widget=forms.HiddenInput)
    user_comments = forms.CharField(
        required=False, widget=forms.Textarea(attrs={"cols": 80, "rows": 5})
    )
    appeal_result = forms.ChoiceField(choices=Appeal_Result_Choices, required=False)
    medicare_someone_to_help = forms.BooleanField(
        required=False,
        label="If you have a medicare plan would you be interested in someone handling the appeal process for you?",
    )
    follow_up_again = forms.BooleanField(required=False, label="Follow up again")
    followup_documents = MultipleFileField(
        required=False,
        label="Optional: Any documents you wish to share",
    )
