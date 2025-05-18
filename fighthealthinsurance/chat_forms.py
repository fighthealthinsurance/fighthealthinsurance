from django import forms


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
