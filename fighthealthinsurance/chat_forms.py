import typing
from typing import Any, Optional

from django import forms
from django.core.files.uploadedfile import UploadedFile

from fighthealthinsurance import consent
from fighthealthinsurance.form_utils import (
    MultipleFileField,
    MultipleFileInput,
    StyledWidgetsMixin,
)
from fighthealthinsurance.forms import REFERRAL_SOURCE_CHOICES
from fighthealthinsurance.models import PolicyDocument
from fighthealthinsurance.state_help import get_states_sorted_by_name

SUPPORTED_POLICY_EXTENSIONS = {".pdf", ".docx", ".txt"}
MAX_POLICY_DOCUMENT_BYTES = 20 * 1024 * 1024

# Magic bytes for file type validation
_PDF_MAGIC = b"%PDF"
_DOCX_MAGIC = b"PK"  # ZIP-based format


def policy_document_extension(name: str) -> str:
    """The file's extension, lowercased with its dot, or "" when it has none."""
    return "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""


def validate_policy_document(file: UploadedFile) -> None:
    """Raise ValidationError unless ``file`` is a policy document we can read:
    a supported extension, at most MAX_POLICY_DOCUMENT_BYTES, and for PDF and
    DOCX the right first bytes."""
    if file.size is not None and file.size > MAX_POLICY_DOCUMENT_BYTES:
        raise forms.ValidationError(
            "File size must be less than 20MB. Please upload a smaller file."
        )
    ext = policy_document_extension(file.name or "")
    if not ext:
        raise forms.ValidationError(
            f"File must have an extension. Please upload a {', '.join(sorted(SUPPORTED_POLICY_EXTENSIONS))} file."
        )
    if ext not in SUPPORTED_POLICY_EXTENSIONS:
        raise forms.ValidationError(
            f"Unsupported file type. Please upload one of: {', '.join(sorted(SUPPORTED_POLICY_EXTENSIONS))}"
        )
    # Magic byte check for PDF and DOCX (stronger than MIME type)
    header = file.read(8)
    file.seek(0)
    if ext == ".pdf" and not header.startswith(_PDF_MAGIC):
        raise forms.ValidationError("The file does not appear to be a valid PDF.")
    if ext == ".docx" and not header.startswith(_DOCX_MAGIC):
        raise forms.ValidationError(
            "The file does not appear to be a valid DOCX document."
        )


class BaseConsentForm(forms.Form):
    """Shared consent fields for forms that collect user info + TOS agreement."""

    first_name = forms.CharField(
        max_length=100,
        required=True,
        widget=forms.TextInput(
            attrs={
                "class": "fhi-field",
                "id": "store_fname",
                "placeholder": "Enter your first name",
            }
        ),
    )

    last_name = forms.CharField(
        max_length=100,
        required=True,
        widget=forms.TextInput(
            attrs={
                "class": "fhi-field",
                "id": "store_lname",
                "placeholder": "Enter your last name",
            }
        ),
    )

    email = forms.EmailField(
        required=True,
        widget=forms.EmailInput(
            attrs={
                "class": "fhi-field",
                "id": "email",
                "placeholder": "Enter your email address",
            }
        ),
    )

    tos_agreement = forms.BooleanField(
        required=True,
        widget=forms.CheckboxInput(attrs={"class": "fhi-check", "id": "tos"}),
    )

    privacy_policy = forms.BooleanField(
        required=True,
        widget=forms.CheckboxInput(attrs={"class": "fhi-check", "id": "privacy"}),
    )

    # Unticked until the person ticks it: the site promises no dark patterns.
    subscribe = forms.BooleanField(
        required=False,
        initial=False,
        widget=forms.CheckboxInput(attrs={"class": "fhi-check", "id": "subscribe"}),
    )


class UnderstandPolicyForm(BaseConsentForm):
    """Form for uploading policy documents with consent"""

    policy_document = forms.FileField(
        required=True,
        widget=forms.FileInput(
            attrs={
                "class": "fhi-field",
                "id": "policy_document",
                "accept": ".pdf,.docx,.txt",
            }
        ),
    )

    document_type = forms.ChoiceField(
        required=True,
        choices=PolicyDocument.DOCUMENT_TYPE_CHOICES,
        initial="summary_of_benefits",
        widget=forms.Select(
            attrs={
                "class": "fhi-field",
                "id": "document_type",
            }
        ),
    )

    PLAN_CATEGORY_CHOICES = (
        ("employer_erisa", "Employer Plan (ERISA) — regulated by Dept. of Labor"),
        (
            "employer_non_erisa",
            "Employer Plan (Non-ERISA, e.g. government/church employer)",
        ),
        ("aca_marketplace", "ACA Marketplace (Healthcare.gov / State Exchange)"),
        ("medicare_traditional", "Medicare (Traditional/Original)"),
        ("medicare_advantage", "Medicare Advantage (Part C)"),
        ("medicaid_chip", "Medicaid / CHIP"),
        ("tricare", "TRICARE (Military)"),
        ("va", "VA Health Care"),
        ("individual_off_exchange", "Individual Plan (Off-Exchange)"),
        ("short_term", "Short-Term Health Plan"),
        ("unknown", "I'm Not Sure"),
    )

    plan_category = forms.ChoiceField(
        required=True,
        choices=PLAN_CATEGORY_CHOICES,
        initial="unknown",
        widget=forms.Select(
            attrs={
                "class": "fhi-field",
                "id": "plan_category",
            }
        ),
    )

    user_question = forms.CharField(
        required=False,
        max_length=1000,
        widget=forms.Textarea(
            attrs={
                "class": "fhi-field",
                "id": "user_question",
                "rows": 3,
                "placeholder": "Optional: What specific question do you have about your policy? (e.g., 'Is my MRI covered?' or 'What are the exclusions for mental health?')",
            }
        ),
    )

    def clean_policy_document(self) -> Optional[UploadedFile]:
        """Validate the uploaded file type and size."""
        file = self.cleaned_data.get("policy_document")
        if file:
            validate_policy_document(file)
        return file


class SharePlanDocumentsForm(StyledWidgetsMixin, forms.Form):
    """Plan documents shared for research. Only the files and the agreement
    are required: no name, and an email only so a removal request finds them."""

    MAX_FILES = 10
    # The optional fields, shown folded under "About the plan".
    ABOUT_THE_PLAN = ("insurer_name", "employer_name", "plan_year", "state", "email")

    # Required is checked in clean_plan_documents: an empty list passes the field.
    plan_documents = MultipleFileField(
        label="Your plan documents",
        widget=MultipleFileInput(attrs={"accept": ".pdf,.docx,.txt"}),
    )
    plan_category = forms.ChoiceField(
        required=False,
        choices=PolicyDocument.PLAN_CATEGORY_CHOICES,
        initial="unknown",
        label="What kind of plan is it?",
    )
    insurer_name = forms.CharField(
        required=False, max_length=200, label="Insurance company"
    )
    employer_name = forms.CharField(
        required=False, max_length=200, label="Employer, if the plan is through a job"
    )
    plan_year = forms.IntegerField(
        required=False,
        min_value=2000,
        max_value=2099,
        label="Plan year",
        error_messages={
            "min_value": "Enter a year from 2000 to 2099.",
            "max_value": "Enter a year from 2000 to 2099.",
            "invalid": "Enter a year from 2000 to 2099.",
        },
    )
    state = forms.ChoiceField(required=False, choices=(), label="State")
    email = forms.EmailField(
        required=False,
        max_length=300,
        label="Your email",
        help_text=(
            "We keep only a scrambled form of it, so we can find these files "
            "if you ask us to delete them. We can't use it to write to you."
        ),
    )
    agree = forms.BooleanField(
        required=True,
        label=consent.SHARING_CHECKBOX,
        error_messages={"required": "Tick the box to agree before you share."},
    )
    # Hidden from people; the view drops a post that fills it in.
    website = forms.CharField(required=False, widget=forms.HiddenInput())

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        state_field = typing.cast(forms.ChoiceField, self.fields["state"])
        state_field.choices = [("", "Choose a state")] + [
            (state.abbreviation, state.name) for state in get_states_sorted_by_name()
        ]

    def clean_plan_documents(self) -> list[UploadedFile]:
        files = [f for f in self.cleaned_data.get("plan_documents") or [] if f]
        if not files:
            raise forms.ValidationError("Choose at least one file to share.")
        if len(files) > self.MAX_FILES:
            raise forms.ValidationError(
                f"You can share up to {self.MAX_FILES} files at a time."
            )
        problems: list[str] = []
        for file in files:
            try:
                validate_policy_document(file)
            except forms.ValidationError as error:
                problems.extend(f"{file.name}: {message}" for message in error.messages)
        if problems:
            raise forms.ValidationError(problems)
        return files

    def about_the_plan_has_errors(self) -> bool:
        return any(name in self.errors for name in self.ABOUT_THE_PLAN)


class UserConsentForm(BaseConsentForm):
    """Form for user TOS consent and personal information collection"""

    phone = forms.CharField(
        max_length=20,
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "fhi-field",
                "id": "phone",
                "placeholder": "Enter your phone number",
            }
        ),
    )

    address = forms.CharField(
        max_length=200,
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "fhi-field",
                "id": "store_street",
                "placeholder": "Enter your street address",
            }
        ),
    )

    city = forms.CharField(
        max_length=100,
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "fhi-field",
                "id": "store_city",
                "placeholder": "Enter your city",
            }
        ),
    )

    state = forms.CharField(
        max_length=50,
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "fhi-field",
                "id": "store_state",
                "placeholder": "Enter your state",
            }
        ),
    )

    zip_code = forms.CharField(
        max_length=20,
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "fhi-field",
                "id": "store_zip",
                "placeholder": "Enter your ZIP code",
            }
        ),
    )

    referral_source = forms.ChoiceField(
        required=False,
        choices=REFERRAL_SOURCE_CHOICES,
        widget=forms.Select(attrs={"class": "fhi-field", "id": "referral_source"}),
    )

    referral_source_details = forms.CharField(
        required=False,
        max_length=500,
        widget=forms.TextInput(
            attrs={
                "class": "fhi-field",
                "id": "referral_source_details",
                "placeholder": "E.g., which search engine, social media platform, or person's name",
            }
        ),
    )

    use_external_models = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(
            attrs={"class": "fhi-check", "id": "use_external_models"}
        ),
    )
