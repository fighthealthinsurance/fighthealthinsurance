from typing import Optional

from django import forms
from django.core.files.uploadedfile import UploadedFile

from fighthealthinsurance.forms import REFERRAL_SOURCE_CHOICES
from fighthealthinsurance.models import PolicyDocument

SUPPORTED_POLICY_EXTENSIONS = {".pdf", ".docx", ".txt"}

# Magic bytes for file type validation
_PDF_MAGIC = b"%PDF"
_DOCX_MAGIC = b"PK"  # ZIP-based format


class BaseConsentForm(forms.Form):
    """Shared consent fields for forms that collect user info + TOS agreement."""

    first_name = forms.CharField(
        max_length=100,
        required=True,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
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
                "class": "form-control",
                "id": "store_lname",
                "placeholder": "Enter your last name",
            }
        ),
    )

    email = forms.EmailField(
        required=True,
        widget=forms.EmailInput(
            attrs={
                "class": "form-control",
                "id": "email",
                "placeholder": "Enter your email address",
            }
        ),
    )

    tos_agreement = forms.BooleanField(
        required=True,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input", "id": "tos"}),
    )

    privacy_policy = forms.BooleanField(
        required=True,
        widget=forms.CheckboxInput(
            attrs={"class": "form-check-input", "id": "privacy"}
        ),
    )

    subscribe = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(
            attrs={"class": "form-check-input", "id": "subscribe"}
        ),
    )


class UnderstandPolicyForm(BaseConsentForm):
    """Form for uploading policy documents with consent"""

    policy_document = forms.FileField(
        required=True,
        widget=forms.FileInput(
            attrs={
                "class": "form-control",
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
                "class": "form-control",
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
                "class": "form-control",
                "id": "plan_category",
            }
        ),
    )

    user_question = forms.CharField(
        required=False,
        max_length=1000,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
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
            if file.size > 20 * 1024 * 1024:
                raise forms.ValidationError(
                    "File size must be less than 20MB. Please upload a smaller file."
                )
            ext = "." + file.name.rsplit(".", 1)[-1].lower() if "." in file.name else ""
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
                raise forms.ValidationError(
                    "The file does not appear to be a valid PDF."
                )
            if ext == ".docx" and not header.startswith(_DOCX_MAGIC):
                raise forms.ValidationError(
                    "The file does not appear to be a valid DOCX document."
                )
        return file


class UserConsentForm(BaseConsentForm):
    """Form for user TOS consent and personal information collection"""

    phone = forms.CharField(
        max_length=20,
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
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
                "class": "form-control",
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
                "class": "form-control",
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
                "class": "form-control",
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
                "class": "form-control",
                "id": "store_zip",
                "placeholder": "Enter your ZIP code",
            }
        ),
    )

    referral_source = forms.ChoiceField(
        required=False,
        choices=REFERRAL_SOURCE_CHOICES,
        widget=forms.Select(attrs={"class": "form-control", "id": "referral_source"}),
    )

    referral_source_details = forms.CharField(
        required=False,
        max_length=500,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "id": "referral_source_details",
                "placeholder": "E.g., which search engine, social media platform, or person's name",
            }
        ),
    )

    use_external_models = forms.BooleanField(
        required=False,
        initial=False,
        widget=forms.CheckboxInput(
            attrs={"class": "form-check-input", "id": "use_external_models"}
        ),
    )
