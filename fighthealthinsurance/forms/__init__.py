import os
import json
import typing
from typing import TYPE_CHECKING

from django import forms
from django.conf import settings
from django.forms import CheckboxInput, ModelForm, Textarea

from django_recaptcha.fields import ReCaptchaField, ReCaptchaV2Checkbox

if TYPE_CHECKING:
    # Typing-only base so mypy knows ``self.fields`` exists. At runtime the
    # mixin stays a plain ``object`` so it doesn't interfere with the form
    # metaclass or MRO of the concrete forms that use it.
    from django.forms import BaseForm as _ReCaptchaMixinBase
else:
    _ReCaptchaMixinBase = object

from fighthealthinsurance.form_utils import *
from fighthealthinsurance.letter_placeholders import (
    blanks_to_name,
    describe_placeholders,
    find_placeholders_as_written,
)
from fighthealthinsurance.models import (
    DenialTypes,
    InsuranceCompany,
    InsurancePlan,
    InterestedProfessional,
    PlanSource,
)

# Referral source choices used across multiple forms
REFERRAL_SOURCE_CHOICES = [
    ("", "-- Please select --"),
    ("Search Engine (Google, Bing, etc.)", "Search Engine (Google, Bing, etc.)"),
    (
        "Social Media (Facebook, Twitter, etc.)",
        "Social Media (Facebook, Twitter, etc.)",
    ),
    ("Friend or Family", "Friend or Family"),
    ("Healthcare Provider", "Healthcare Provider"),
    ("News Article or Blog", "News Article or Blog"),
    ("Other", "Other"),
]


class ReCaptchaOptionalMixin(_ReCaptchaMixinBase):
    """Adds an optionally-enforced reCAPTCHA field to a form.

    Order matters: list this mixin **before** ``forms.Form`` /
    ``forms.ModelForm`` in the base classes, and declare a placeholder
    ``captcha`` field so the form metaclass collects it::

        class MyForm(ReCaptchaOptionalMixin, forms.ModelForm):
            captcha = forms.CharField(required=False, widget=forms.HiddenInput())

    The real ReCaptchaField is swapped in by this mixin's ``__init__``. Django's
    ``BaseForm.__init__`` does not call ``super().__init__()``, so the mixin
    must precede the form base in the MRO for its ``__init__`` to run at all;
    listing it *after* the form base silently leaves the hidden no-op field in
    place and disables the captcha.

    The placeholder is a hidden no-op CharField and is swapped to a real
    ReCaptchaField at instance construction time when Django settings have both
    RECAPTCHA_PUBLIC_KEY and RECAPTCHA_PRIVATE_KEY configured and
    RECAPTCHA_TESTING is not enabled. Gating on django.conf.settings (rather
    than os.environ at import time) keeps a single source of truth and makes
    the behavior testable via override_settings.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self._is_recaptcha_enabled():
            self.fields["captcha"] = ReCaptchaField(widget=ReCaptchaV2Checkbox())

    @staticmethod
    def _is_recaptcha_enabled() -> bool:
        """Return True when reCAPTCHA should be enforced.

        Requires both RECAPTCHA_PUBLIC_KEY and RECAPTCHA_PRIVATE_KEY to be
        set (non-empty) and RECAPTCHA_TESTING to not be enabled.
        """
        if getattr(settings, "RECAPTCHA_TESTING", False):
            return False
        return bool(
            getattr(settings, "RECAPTCHA_PUBLIC_KEY", "")
            and getattr(settings, "RECAPTCHA_PRIVATE_KEY", "")
        )


# Actual forms
class _InterestedProfessionalFieldsForm(forms.ModelForm):
    """Shared field declarations for the interested-professional intake forms.

    Not used directly. Concrete subclasses layer on their own spam protection:
    :class:`InterestedProfessionalForm` (the on-site /pro_version form) adds
    reCAPTCHA via ``ReCaptchaOptionalMixin``, while
    :class:`ExternalInterestedProfessionalForm` (the off-site fightpaperwork.com
    classic form) uses a honeypot because its host page runs no JavaScript.
    """

    business_name = forms.CharField(required=False)
    address = forms.CharField(
        required=False,
    )
    comments = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "placeholder": "ENTER YOUR COMMENTS HERE. WE WELCOME FEEDBACK!",
                "class": "comments form-textarea-wide",
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
                "class": "most_common_denial form-textarea-medium",
            }
        ),
    )

    class Meta:
        model = InterestedProfessional
        # Positive allow-list of the public intake fields. A `fields` list
        # (rather than `exclude`) means any model field NOT named here — the
        # payment/state flags and, crucially, the internal proconnector_*
        # workflow fields (proconnector_attempted, proconnector_skipped, ...) —
        # can never be mass-assigned from a public POST. That matters because a
        # crafted submission setting proconnector_attempted/skipped=True would
        # otherwise drop the lead out of proconnector.processable_queryset().
        fields = [
            "name",
            "email",
            "business_name",
            "address",
            "comments",
            "phone_number",
            "job_title_or_provider_type",
            "most_common_denial",
        ]


class InterestedProfessionalForm(
    ReCaptchaOptionalMixin, _InterestedProfessionalFieldsForm
):
    captcha = forms.CharField(required=False, widget=forms.HiddenInput())


class ExternalInterestedProfessionalForm(_InterestedProfessionalFieldsForm):
    """Off-site (fightpaperwork.com) classic-HTML variant of the interest form.

    Captures the same fields as :class:`InterestedProfessionalForm` but drops
    reCAPTCHA: the static Fight Paperwork page runs no JavaScript, so the form
    is submitted as a classic top-level POST and bot protection is a hidden
    honeypot instead. ``website`` is invisible to humans; any non-empty value
    marks the submission as a bot, which the view drops silently.
    """

    website = forms.CharField(required=False, widget=forms.HiddenInput())


class DeleteDataForm(forms.Form):
    """Base form for data deletion — email-only, no captcha.

    Used by `AdminDeleteDataView` (staff-authenticated deletion flow).
    The public HTML flow uses `PublicDeleteDataForm` (below) which adds
    a reCAPTCHA field.
    """

    email = forms.EmailField(required=True)


class PublicDeleteDataForm(ReCaptchaOptionalMixin, DeleteDataForm):
    """Public-facing delete data form with reCAPTCHA protection.

    Used by the unauthenticated public deletion request flow to prevent
    bots from spamming deletion request emails. See ReCaptchaOptionalMixin
    for how the captcha field is conditionally enforced.
    """

    captcha = forms.CharField(required=False, widget=forms.HiddenInput())


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
    use_external_models = forms.BooleanField(required=False, initial=True)
    denial_text = forms.CharField(required=True)
    email = forms.EmailField(required=True)
    # Unticked until the person ticks it: the site promises no dark patterns.
    subscribe = forms.BooleanField(required=False, initial=False)


class DenialForm(BaseDenialForm):
    pass


class ProDenialForm(BaseDenialForm):
    # In pro we can fetch email from the patient object
    primary_professional = forms.CharField(required=False)
    patient_id = forms.CharField(required=False)
    insurance_company = forms.CharField(required=False)
    insurance_company_obj = forms.ModelChoiceField(
        queryset=InsuranceCompany.objects.all(),
        required=False,
    )
    insurance_plan_obj = forms.ModelChoiceField(
        queryset=InsurancePlan.objects.all(),
        required=False,
    )
    patient_visible = forms.BooleanField(required=False)
    denial_id = forms.IntegerField(required=False)


class DenialRefForm(StyledWidgetsMixin, forms.Form):
    denial_id = forms.IntegerField(required=True, widget=forms.HiddenInput())
    email = forms.CharField(required=True, widget=forms.HiddenInput())
    semi_sekret = forms.CharField(required=True, widget=forms.HiddenInput())


class HealthHistory(DenialRefForm):
    # health_history.html renders no checkbox for either consent flag, and an
    # unchecked BooleanField is absent from the POST and cleans to False, which
    # _update_denial reads as a decision. So every Next used to revoke whatever
    # the person had chosen.
    #
    # They cannot simply be dropped either: rest_serializers builds
    # HealthHistoryFormSerializer from this form and drf_braces strips anything
    # the form does not declare, so removing them stopped the API revoking a
    # consent it was explicitly told to revoke, while still answering 201.
    # They stay here for the API, and PlanDocumentsView drops the ones its own
    # page never asked about.
    health_history = forms.CharField(required=False)
    # A digest of the history this page was rendered with, so the save can
    # tell a stale page passing through from someone actually editing. The
    # digest rather than the text: a hidden field holding the history itself
    # would put it in the DOM and in the POST body a second time, and it is
    # the most sensitive column on the row.
    health_history_seen = forms.CharField(required=False, widget=forms.HiddenInput)
    health_history_anonymized = forms.BooleanField(required=False)
    include_provided_health_history_in_appeal = forms.BooleanField(required=False)
    # The answer to the box this page renders. Declared here and not on
    # DenialRefForm on purpose: ProPostInferedForm also descends from that
    # base, its optional booleans clean to False when a caller omits them,
    # and find_next_steps persists what it is given, so a professional
    # posting next steps would silently revoke a consent nobody asked them
    # about.
    health_history_consent = forms.BooleanField(required=False)


class PlanDocumentsForm(DenialRefForm):
    plan_documents = MultipleFileField(required=False)


class ChooseAppealForm(DenialRefForm):
    appeal_text = forms.CharField(
        widget=forms.Textarea(attrs={"class": "appeal_text"}), required=True
    )
    # Optional id of the ProposedAppeal row generated for this draft.
    # When present, ChooseAppealHelper uses it to preserve model attribution
    # even after sub_in_appeals rewrites the raw text the user sees.
    proposed_appeal_id = forms.IntegerField(required=False, widget=forms.HiddenInput())
    # Set by the browser when the draft's streaming frame said it was never
    # stored (id "unknown" / save_failed): the stored drafts are then no
    # evidence of which model produced it, so sole-draft inference is off.
    draft_unsaved = forms.BooleanField(required=False, widget=forms.HiddenInput())
    # Set by the browser once the textarea is changed, so the chosen row can
    # say whether the draft was sent as generated (ProposedAppeal.editted).
    editted = forms.BooleanField(required=False, widget=forms.HiddenInput())
    # The ids of the drafts on screen when the pick was made (a JSON list of
    # ints), so the usage dashboard's "presented" can count what was shown
    # rather than everything generated. Anything unparseable is dropped.
    presented_ids = forms.CharField(required=False, widget=forms.HiddenInput())

    # Bounded: a page never shows more than a few dozen drafts.
    MAX_PRESENTED_IDS = 100

    def clean_presented_ids(self) -> typing.Optional[typing.List[int]]:
        raw = (self.cleaned_data.get("presented_ids") or "").strip()
        if not raw:
            return None
        try:
            values = json.loads(raw)
        except (ValueError, RecursionError):
            # Not JSON, or nested past the parser's depth: not a report.
            return None
        if not isinstance(values, list):
            return None
        ids: typing.List[int] = []
        for value in values[: self.MAX_PRESENTED_IDS]:
            try:
                candidate = int(value)
            except (TypeError, ValueError, OverflowError):
                # OverflowError: a JSON 1e999 parses to inf.
                continue
            # A row id fits a signed 64-bit column; anything else is junk
            # that sqlite would refuse as a query parameter.
            if 0 < candidate < 2**63:
                ids.append(candidate)
        # A parsed list is a report even when empty ("nothing stored was on
        # screen"); None is kept for "nobody said".
        return ids


class ChooseEscalationLetterForm(DenialRefForm):
    escalation_uuid = forms.UUIDField(required=True, widget=forms.HiddenInput())
    letter_text = forms.CharField(
        widget=forms.Textarea(attrs={"class": "appeal_text"}), required=True
    )


class FaxForm(DenialRefForm):
    name = forms.CharField(
        required=True,
        label="Your full name",
        help_text="This will appear on the fax cover page.",
        widget=forms.TextInput(attrs={"placeholder": "e.g., Jane Smith"}),
    )
    insurance_company = forms.CharField(
        required=True,
        label="Insurance company name",
        help_text="The company receiving this fax.",
        widget=forms.TextInput(attrs={"placeholder": "e.g., Aetna, Blue Cross"}),
    )
    fax_phone = forms.CharField(
        required=True,
        label="Fax number for appeals",
        help_text="Check your denial letter for the appeals fax number.",
        widget=forms.TextInput(
            attrs={"placeholder": "e.g., 1-800-555-1234", "type": "tel"}
        ),
    )
    completed_appeal_text = forms.CharField(
        widget=forms.Textarea(attrs={"class": "appeal_text"}),
        required=True,
        label="Your appeal letter",
    )
    # Some of what the blank check finds is not a blank: an acronym in
    # brackets like [ERISA], a name typed inside the brackets, a line to sign
    # on. This box, "Send it as it is", says yes to the blanks the page names,
    # and to the ones the person already said yes to, and to nothing else:
    # its value is that list (JSON, each blank exactly as the letter has it),
    # so a ticked box posts the list and an unticked one posts nothing. A
    # letter with more blanks than a message names is named ten at a time. The appeal page's "Send anyway" posts a list of its own
    # under the same name, in a hidden field. A letter with blanks is faxed
    # only when every blank in it is on a posted list.
    # The box is off the form (see __init__) until clean() holds a letter for
    # its blanks, so it shows under the letter on the page that names them,
    # and on no other: a page turned back for something else, like a name of
    # only spaces, has none. It never comes back ticked.
    approved_placeholders = forms.BooleanField(
        required=False,
        label="Send it as it is: I've checked these are not blanks",
        label_suffix="",
        widget=forms.CheckboxInput(check_test=lambda _: False),
        template_name="partials/check_row_field.html",
    )
    include_provided_health_history = forms.BooleanField(
        required=False,
        label="Include my health history in the fax",
        help_text="If you provided health history earlier, include it with your appeal.",
    )
    # Note: we don't have fax_pwyw etc. so we don't overload.

    # How many blanks the person said to fax as they are; 0 when none.
    placeholders_sent_as_they_are: int = 0

    def __init__(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        super().__init__(*args, **kwargs)
        self._send_as_it_is_box = self.fields.pop("approved_placeholders")

    def _approved_placeholders(self) -> set[str]:
        """Every blank on a list posted as approved: by the ticked box, by
        "Send anyway", or both. Anything that is not a JSON list of strings
        approves nothing, so a bare "1" or "on" lets no blank through."""
        name = self.add_prefix("approved_placeholders")
        getlist = getattr(self.data, "getlist", None)
        if getlist is not None:
            posted = getlist(name)
        else:
            value = self.data.get(name)
            posted = value if isinstance(value, list) else [value]
        approved: set[str] = set()
        for raw in posted:
            if not isinstance(raw, str):
                continue
            try:
                values = json.loads(raw)
            except (ValueError, RecursionError):
                # Not JSON, or nested past the parser's depth.
                continue
            if isinstance(values, list):
                approved.update(value for value in values if isinstance(value, str))
        return approved

    def _offer_to_send_as_it_is(self, blanks: list[str]) -> None:
        """Put the box back on the form, just under the letter, holding
        exactly ``blanks``."""
        box = self._send_as_it_is_box
        box.widget.attrs["value"] = json.dumps(blanks)
        letter_id = self["completed_appeal_text"].auto_id
        if letter_id:
            # The box's label says "these"; a screen reader reads it with
            # the list of blanks, the letter's error, which has this id.
            box.widget.attrs["aria-describedby"] = f"{letter_id}_error"
        fields = list(self.fields.items())
        names = [name for name, _ in fields]
        at = (
            names.index("completed_appeal_text") + 1
            if "completed_appeal_text" in names
            else len(fields)
        )
        fields.insert(at, ("approved_placeholders", box))
        self.fields = dict(fields)

    def clean(self) -> typing.Optional[dict[str, typing.Any]]:
        """A letter with blanks left in it, like [Your Name], is faxed only
        when the person has said yes to every one of them.

        The insurance company would get the blanks exactly as written. The
        appeal page's script names them before the form is sent; this holds
        for a browser that never ran it, and for a blank nobody said yes to.
        The same pattern list drives both.
        """
        cleaned_data = super().clean()
        text = self.cleaned_data.get("completed_appeal_text")
        if not text:
            return cleaned_data
        blanks = find_placeholders_as_written(text)
        if not blanks:
            return cleaned_data
        approved = self._approved_placeholders()
        if all(blank in approved for blank in blanks):
            self.placeholders_sent_as_they_are = len(blanks)
            return cleaned_data
        # A long list is named ten at a time, and the box says yes only to
        # what the person has been shown: the blanks named here, and the
        # ones they said yes to before, which stay said yes to.
        to_name = blanks_to_name(text, approved)
        self._offer_to_send_as_it_is(to_name.send_as_it_is)
        self.add_error(
            "completed_appeal_text",
            forms.ValidationError(
                "Fill in these blanks before we fax your letter: "
                f"{describe_placeholders(to_name.shown)}. "
                "Your insurance company would get them exactly as written. "
                "Replace each one with your details, or delete it if it "
                "doesn't apply, then send the fax again. If you've checked "
                "and these are not blanks, tick the box under your letter to "
                "send it as it is.",
                code="unfilled_placeholders",
            ),
        )
        return cleaned_data


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
        label="Type of denial",
        help_text="Select all that apply. If unsure, leave blank.",
    )
    denial_type_text = forms.CharField(
        required=False,
        label="Other denial type",
        help_text="If your denial type isn't listed above, describe it here.",
        widget=forms.TextInput(
            attrs={"placeholder": "e.g., Out of network, Experimental treatment"}
        ),
    )
    plan_id = forms.CharField(
        required=False,
        label="Plan ID / Member ID",
        help_text="Usually found on your insurance card.",
        widget=forms.TextInput(attrs={"placeholder": "e.g., ABC123456789"}),
    )
    claim_id = forms.CharField(
        required=False,
        label="Claim ID / Reference Number",
        help_text="From your denial letter or Explanation of Benefits (EOB).",
        widget=forms.TextInput(attrs={"placeholder": "e.g., CLM-2024-12345"}),
    )
    date_of_service = forms.CharField(
        required=False,
        label="Date of service",
        help_text="When the denied service was provided or requested.",
        widget=forms.TextInput(
            attrs={"placeholder": "e.g., 01/15/2024 or January 2024"}
        ),
    )
    insurance_company = forms.CharField(
        required=False,
        label="Insurance company",
        help_text="The name of your health insurance provider.",
        widget=forms.TextInput(
            attrs={"placeholder": "e.g., Blue Cross, Aetna, UnitedHealthcare"}
        ),
    )
    # Optional structured company selection
    insurance_company_obj = forms.ModelChoiceField(
        queryset=InsuranceCompany.objects.all(),
        required=False,
        label="Select insurance company (optional)",
        help_text="Choose from list if available, otherwise use text field above.",
        widget=forms.Select(attrs={"class": "insurance-company-select"}),
    )
    # Optional structured plan selection
    insurance_plan_obj = forms.ModelChoiceField(
        queryset=InsurancePlan.objects.all(),
        required=False,
        label="Select specific plan (optional)",
        help_text="For state-specific plans like Medicaid, choose if available.",
        widget=forms.Select(attrs={"class": "insurance-plan-select"}),
    )
    plan_source = forms.ModelMultipleChoiceField(
        queryset=PlanSource.objects.all(),
        required=False,
        label="How do you get your insurance?",
        help_text="Select all that apply.",
    )
    employer_name = forms.CharField(
        required=False,
        label="Employer name (if employer-provided insurance)",
        widget=forms.TextInput(attrs={"placeholder": "e.g., Acme Corporation"}),
    )
    denial_date = forms.DateField(
        required=False,
        label="Date of denial letter",
        help_text="When was the denial letter dated?",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    your_state = forms.CharField(
        max_length=2,
        required=False,
        label="Your state",
        help_text="Two-letter state code (e.g., CA, NY, TX).",
        widget=forms.TextInput(
            attrs={"placeholder": "CA", "maxlength": "2", "class": "form-input-state"}
        ),
    )
    procedure = forms.CharField(
        max_length=200,
        required=False,
        label="Denied procedure or treatment",
        help_text="What service, procedure, or treatment was denied?",
        widget=forms.TextInput(
            attrs={"placeholder": "e.g., MRI, Physical therapy, Surgery"}
        ),
    )
    diagnosis = forms.CharField(
        max_length=200,
        required=False,
        label="Related diagnosis or condition",
        help_text="The medical condition or reason for needing the treatment. Can include any relevant personal health factors.",
        widget=forms.TextInput(
            attrs={"placeholder": "e.g., Chronic back pain, Diabetes, Gender dysphoria"}
        ),
    )


class PostInferedForm(ReCaptchaOptionalMixin, BasePostInferedForm):
    """Public appeal form with conditionally-enforced reCAPTCHA.

    Previously decided at import time from ``os.environ``, which made the
    captcha depend on process-wide state rather than the active
    configuration. See ReCaptchaOptionalMixin for how the field is
    conditionally enforced from ``django.conf.settings`` instead.
    """

    captcha = forms.CharField(required=False, widget=forms.HiddenInput())


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


# Form for sending mailing list emails
class SendMailingListMailForm(forms.Form):
    subject = forms.CharField(
        required=True,
        max_length=200,
        widget=forms.TextInput(
            attrs={"class": "form-control", "placeholder": "Email subject"}
        ),
    )
    html_content = forms.CharField(
        required=True,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 15,
                "placeholder": "HTML version of the email",
            }
        ),
        label="HTML Content",
    )
    text_content = forms.CharField(
        required=True,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 15,
                "placeholder": "Plain text version of the email",
            }
        ),
        label="Text Content",
    )
    test_email = forms.EmailField(
        required=False,
        widget=forms.EmailInput(
            attrs={
                "class": "form-control",
                "placeholder": "Optional: Send test email to this address first",
            }
        ),
        label="Test Email (optional)",
        help_text="If provided, the email will only be sent to this address for testing.",
    )
