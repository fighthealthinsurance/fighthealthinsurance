import os

from django import forms
from django.forms import CheckboxInput, ModelForm, Textarea

from django_recaptcha.fields import ReCaptchaField, ReCaptchaV2Checkbox

from fighthealthinsurance.form_utils import *
from fighthealthinsurance.models import (
    DenialTypes,
    InsuranceCallLog,
    InsuranceCompany,
    InsurancePlan,
    InterestedProfessional,
    PatientEvidence,
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
    subscribe = forms.BooleanField(required=False, initial=True)


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
    include_provided_health_history = forms.BooleanField(
        required=False,
        label="Include my health history in the fax",
        help_text="If you provided health history earlier, include it with your appeal.",
    )
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


# Patient Dashboard Forms - Call Log & Evidence Tracking


class InsuranceCallLogForm(forms.ModelForm):
    """Form for patients to log phone calls with their insurance company."""

    call_date = forms.DateTimeField(
        required=True,
        widget=forms.DateTimeInput(
            attrs={
                "class": "form-control",
                "type": "datetime-local",
            }
        ),
        label="Date and Time of Call",
    )
    call_type = forms.ChoiceField(
        choices=InsuranceCallLog.CALL_TYPE_CHOICES,
        required=True,
        widget=forms.Select(attrs={"class": "form-select"}),
        label="Type of Call",
    )
    department = forms.CharField(
        required=False,
        max_length=200,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "e.g., Claims, Appeals, Member Services",
            }
        ),
        label="Department Contacted",
    )
    representative_name = forms.CharField(
        required=False,
        max_length=200,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "Name of the person you spoke with",
            }
        ),
        label="Representative Name",
    )
    representative_id = forms.CharField(
        required=False,
        max_length=100,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "Employee ID or badge number if provided",
            }
        ),
        label="Representative ID",
    )
    reference_number = forms.CharField(
        required=False,
        max_length=100,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "Call reference or confirmation number",
            }
        ),
        label="Reference Number",
        help_text="This is crucial - always ask for a reference number!",
    )
    case_number = forms.CharField(
        required=False,
        max_length=100,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "Case or ticket number if assigned",
            }
        ),
        label="Case/Ticket Number",
    )
    reason_for_call = forms.CharField(
        required=True,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 3,
                "placeholder": "Why did you call? What question or issue did you need to address?",
            }
        ),
        label="Reason for Call",
    )
    key_statements = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 4,
                "placeholder": 'Important things the representative said. Use quotes when possible: "They told me..."',
            }
        ),
        label="Key Statements",
        help_text="Document exactly what they told you - this can be used in appeals.",
    )
    promises_made = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 3,
                "placeholder": "Any commitments or promises made (e.g., callback, approval timeline)",
            }
        ),
        label="Promises/Commitments Made",
    )
    call_notes = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 3,
                "placeholder": "Any other notes about the call",
            }
        ),
        label="Additional Notes",
    )
    outcome = forms.ChoiceField(
        choices=InsuranceCallLog.OUTCOME_CHOICES,
        required=True,
        widget=forms.Select(attrs={"class": "form-select"}),
        label="Call Outcome",
    )
    follow_up_date = forms.DateField(
        required=False,
        widget=forms.DateInput(
            attrs={
                "class": "form-control",
                "type": "date",
            }
        ),
        label="Follow-up Date",
        help_text="When should you follow up on this?",
    )
    follow_up_notes = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 2,
                "placeholder": "What do you need to do or check on the follow-up date?",
            }
        ),
        label="Follow-up Notes",
    )
    call_duration_minutes = forms.IntegerField(
        required=False,
        min_value=0,
        widget=forms.NumberInput(
            attrs={
                "class": "form-control",
                "placeholder": "Minutes",
            }
        ),
        label="Call Duration (minutes)",
    )
    wait_time_minutes = forms.IntegerField(
        required=False,
        min_value=0,
        widget=forms.NumberInput(
            attrs={
                "class": "form-control",
                "placeholder": "Minutes on hold",
            }
        ),
        label="Wait/Hold Time (minutes)",
    )
    include_in_appeal = forms.BooleanField(
        required=False,
        initial=False,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
        label="Include in appeal documentation",
        help_text="Check this if you want to reference this call in your appeal letter.",
    )

    class Meta:
        model = InsuranceCallLog
        fields = [
            "call_date",
            "call_type",
            "department",
            "representative_name",
            "representative_id",
            "reference_number",
            "case_number",
            "reason_for_call",
            "key_statements",
            "promises_made",
            "call_notes",
            "outcome",
            "follow_up_date",
            "follow_up_notes",
            "call_duration_minutes",
            "wait_time_minutes",
            "include_in_appeal",
        ]


class PatientEvidenceForm(forms.ModelForm):
    """Form for patients to upload and track evidence for their appeals."""

    evidence_type = forms.ChoiceField(
        choices=PatientEvidence.EVIDENCE_TYPE_CHOICES,
        required=True,
        widget=forms.Select(attrs={"class": "form-select"}),
        label="Type of Evidence",
    )
    title = forms.CharField(
        required=True,
        max_length=300,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "Brief title for this evidence",
            }
        ),
        label="Title",
    )
    description = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 3,
                "placeholder": "Describe what this evidence shows and why it's relevant",
            }
        ),
        label="Description",
    )
    date_of_evidence = forms.DateField(
        required=False,
        widget=forms.DateInput(
            attrs={
                "class": "form-control",
                "type": "date",
            }
        ),
        label="Date of Document/Evidence",
        help_text="The date on the document or when it was created",
    )
    file = forms.FileField(
        required=False,
        widget=forms.FileInput(
            attrs={
                "class": "form-control",
                "accept": ".pdf,.png,.jpg,.jpeg,.doc,.docx,.txt",
            }
        ),
        label="Upload File",
        help_text="Supported formats: PDF, images, Word documents, text files",
    )
    text_content = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 5,
                "placeholder": "Enter text content, notes, or paste copied information here",
            }
        ),
        label="Text Content/Notes",
        help_text="You can paste text directly or add your own notes",
    )
    source = forms.CharField(
        required=False,
        max_length=300,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "e.g., MyChart portal, Insurance website, Email",
            }
        ),
        label="Source",
        help_text="Where did you get this document or information?",
    )
    include_in_appeal = forms.BooleanField(
        required=False,
        initial=False,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
        label="Include in appeal documentation",
        help_text="Check this to include this evidence when building your appeal.",
    )

    class Meta:
        model = PatientEvidence
        fields = [
            "evidence_type",
            "title",
            "description",
            "date_of_evidence",
            "file",
            "text_content",
            "source",
            "include_in_appeal",
        ]

    def clean_file(self):
        """
        Validate uploaded file for size and type.

        Security checks:
        - Maximum file size: 10MB
        - Allowed MIME types: PDF, images, Word docs, text files
        """
        file = self.cleaned_data.get("file")
        if file:
            # File size validation (10MB limit)
            max_size = 10 * 1024 * 1024  # 10MB in bytes
            if file.size > max_size:
                raise forms.ValidationError(
                    f"File size exceeds 10MB limit. Your file is {file.size / (1024 * 1024):.1f}MB."
                )

            # MIME type validation
            allowed_types = [
                "application/pdf",  # PDF
                "image/jpeg",  # JPEG images
                "image/jpg",  # JPEG images (alternative)
                "image/png",  # PNG images
                "image/gif",  # GIF images
                "image/webp",  # WebP images
                "application/msword",  # DOC
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # DOCX
                "text/plain",  # TXT
            ]

            if file.content_type not in allowed_types:
                raise forms.ValidationError(
                    f'File type "{file.content_type}" is not allowed. '
                    f"Allowed types: PDF, images (JPEG, PNG, GIF, WebP), Word documents, and text files."
                )

        return file


class CallLogFilterForm(forms.Form):
    """Form for filtering call logs in the patient dashboard."""

    call_type = forms.ChoiceField(
        choices=[("", "All Types")] + list(InsuranceCallLog.CALL_TYPE_CHOICES),
        required=False,
        widget=forms.Select(attrs={"class": "form-select form-select-sm"}),
    )
    outcome = forms.ChoiceField(
        choices=[("", "All Outcomes")] + list(InsuranceCallLog.OUTCOME_CHOICES),
        required=False,
        widget=forms.Select(attrs={"class": "form-select form-select-sm"}),
    )
    date_from = forms.DateField(
        required=False,
        widget=forms.DateInput(
            attrs={
                "class": "form-control form-control-sm",
                "type": "date",
            }
        ),
    )
    date_to = forms.DateField(
        required=False,
        widget=forms.DateInput(
            attrs={
                "class": "form-control form-control-sm",
                "type": "date",
            }
        ),
    )


class EvidenceFilterForm(forms.Form):
    """Form for filtering evidence in the patient dashboard."""

    evidence_type = forms.ChoiceField(
        choices=[("", "All Types")] + list(PatientEvidence.EVIDENCE_TYPE_CHOICES),
        required=False,
        widget=forms.Select(attrs={"class": "form-select form-select-sm"}),
    )
    include_in_appeal = forms.ChoiceField(
        choices=[
            ("", "All"),
            ("true", "Included in Appeal"),
            ("false", "Not Included"),
        ],
        required=False,
        widget=forms.Select(attrs={"class": "form-select form-select-sm"}),
    )
