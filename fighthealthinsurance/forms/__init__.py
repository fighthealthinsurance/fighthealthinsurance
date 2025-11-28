import os

from django import forms
from django.forms import ModelForm, Textarea, CheckboxInput

from fighthealthinsurance.models import InterestedProfessional

from django_recaptcha.fields import ReCaptchaField, ReCaptchaV2Checkbox
from fighthealthinsurance.form_utils import *
from fighthealthinsurance.models import (
    DenialTypes,
    InterestedProfessional,
    PlanSource,
)


# Actual forms
class InterestedProfessionalForm(forms.ModelForm):
    business_name = forms.CharField(required=False)
    address = forms.CharField(
        required=False,
    )
    comments = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "placeholder": "ENTER YOUR COMMENTS HERE. WE WELCOME FEEDBACK!",
                "style": "width: 50em; height: 5em; display: block",
                "class": "comments",
            }
        ),
    )
    phone_number = forms.CharField(required=False)
    job_title_or_provider_type = forms.CharField(required=False)
    most_common_denial = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "placeholder": "ENTER COMMON DENIALS HERE",
                "style": "width: 50em; height: 3em; display: block",
                "class": "most_common_denial",
            }
        ),
    )
    clicked_for_paid = forms.BooleanField(
        initial=True,
        required=False,
        label="Optional: Pay $10 now to get 3-months of the beta when we launch the professional version while we figure out what/if folks will pay for it.",
        widget=forms.CheckboxInput(),
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


class BaseDenialForm(forms.Form):
    zip = forms.CharField(required=False)
    pii = forms.BooleanField(required=True)
    tos = forms.BooleanField(required=True)
    privacy = forms.BooleanField(required=True)
    store_raw_email = forms.BooleanField(required=False)
    use_external_models = forms.BooleanField(required=False)
    denial_text = forms.CharField(required=True)
    email = forms.EmailField(required=True)


class DenialForm(BaseDenialForm):
    pass


class ProDenialForm(BaseDenialForm):
    # In pro we can fetch email from the patient object
    primary_professional = forms.CharField(required=False)
    patient_id = forms.CharField(required=False)
    insurance_company = forms.CharField(required=False)
    patient_visible = forms.BooleanField(required=False)
    denial_id = forms.IntegerField(required=False)


class DenialRefForm(forms.Form):
    denial_id = forms.IntegerField(required=True, widget=forms.HiddenInput())
    email = forms.CharField(required=True, widget=forms.HiddenInput())
    semi_sekret = forms.CharField(required=True, widget=forms.HiddenInput())


class HealthHistory(DenialRefForm):
    health_history = forms.CharField(required=False)
    health_history_anonymized = forms.BooleanField(required=False)
    include_provided_health_history_in_appeal = forms.BooleanField(required=False)


class PlanDocumentsForm(DenialRefForm):
    plan_documents = MultipleFileField(required=False)


class ChooseAppealForm(DenialRefForm):
    appeal_text = forms.CharField(
        widget=forms.Textarea(attrs={"class": "appeal_text"}), required=True
    )


class FaxForm(DenialRefForm):
    name = forms.CharField(required=True, label="Your name (for the cover page)")
    insurance_company = forms.CharField(required=True)
    fax_phone = forms.CharField(required=True)
    completed_appeal_text = forms.CharField(
        widget=forms.Textarea(attrs={"class": "appeal_text"}), required=True
    )
    include_provided_health_history = forms.BooleanField(required=False)
    # Note: we don't have fax_pwyw etc. so we don't overload.


class EntityExtractForm(DenialRefForm):
    """Entity Extraction form."""


class FaxResendForm(forms.Form):
    fax_phone = forms.CharField(required=True)
    uuid = forms.UUIDField(required=True, widget=forms.HiddenInput)
    hashed_email = forms.CharField(required=True, widget=forms.HiddenInput)


class BasePostInferedForm(DenialRefForm):
    """The form to double check what we inferred. This leads to our next steps /
    FindNextSteps."""

    # Send denial id and e-mail back that way people can't just change the ID
    # and get someone elses denial.
    denial_id = forms.IntegerField(required=True, widget=forms.HiddenInput())
    email = forms.CharField(required=True, widget=forms.HiddenInput())
    denial_type = forms.ModelMultipleChoiceField(
        queryset=DenialTypes.objects.all(),
        required=False,
        label="Denial type (if known)",
    )
    denial_type_text = forms.CharField(
        required=False,
        label="Denial Type (text, you can type if the above categories don't match the denial type)",
    )
    plan_id = forms.CharField(required=False)
    claim_id = forms.CharField(required=False)
    date_of_service = forms.CharField(required=False)
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
        label="What is your denied procedure/treatment?",
        required=False,
    )
    diagnosis = forms.CharField(
        max_length=200,
        label="What is the diagnosis (if any) associated with the request?"
        + "Does not need to be a disease it can be any number of personal factors, "
        + 'including things like "high risk homosexual behavior" (yeah that\'s a real one)',
        required=False,
    )


class PostInferedForm(BasePostInferedForm):
    captcha = forms.CharField(required=False, widget=forms.HiddenInput())
    # Instead of the default behaviour we skip the recaptcha field entirely for dev.
    if "RECAPTCHA_PUBLIC_KEY" in os.environ and (
        "RECAPTCHA_TESTING" not in os.environ
        or os.environ["RECAPTCHA_TESTING"].lower() != "true"
    ):
        captcha = ReCaptchaField(widget=ReCaptchaV2Checkbox())


class ProPostInferedForm(BasePostInferedForm):
    single_case = forms.BooleanField(required=False)
    in_network = forms.BooleanField(required=False)
    appeal_fax_number = forms.CharField(required=False)
    include_provided_health_history_in_appeal = forms.BooleanField(required=False)


class FollowUpTestForm(forms.Form):
    email = forms.CharField(required=True)


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
    quote = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"cols": 80, "rows": 5}),
        label="Do you have a quote of your experience you'd be willing to share?",
    )
    use_quote = forms.BooleanField(required=False, label="Can we use/share your quote?")
    name_for_quote = forms.CharField(required=False, label="Name to be used with quote")
    email = forms.CharField(
        required=False, label="Your email if we can follow-up with you more"
    )
    appeal_result = forms.ChoiceField(choices=Appeal_Result_Choices, required=False)
    medicare_someone_to_help = forms.BooleanField(
        required=False,
        label="If you have a medicare plan, would you be interested in someone handling the appeal process for you?",
    )
    follow_up_again = forms.BooleanField(
        required=False, label="Would you like an automated follow-up again"
    )
    followup_documents = MultipleFileField(
        required=False,
        label="Optional: Any documents you wish to share",
    )


# New form for activating pro users
class ActivateProForm(forms.Form):
    phonenumber = forms.CharField(required=True)
