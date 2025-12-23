from django import forms
from fighthealthinsurance.forms import REFERRAL_SOURCE_CHOICES


class UserConsentForm(forms.Form):
    """Form for user TOS consent and personal information collection"""

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
