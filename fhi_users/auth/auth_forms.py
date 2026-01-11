from django import forms
from fhi_users.auth.auth_utils import (
    normalize_phone_number,
)


class LoginForm(forms.Form):
    username = forms.CharField(required=True)
    password = forms.CharField(required=True)
    # We need one of the domain or phone to be not null
    domain = forms.CharField(required=False)
    phone = forms.CharField(required=False)

    def clean_phone(self):
        phone = self.cleaned_data.get("phone")
        if phone:
            return normalize_phone_number(phone)
        return phone


class TOTPForm(forms.Form):
    username = forms.CharField(required=True)
    # We need one of the domain or phone to be not null
    domain = forms.CharField(required=False)
    phone = forms.CharField(required=False)
    totp = forms.CharField(required=True)

    def clean_phone(self):
        phone = self.cleaned_data.get("phone")
        if phone:
            return normalize_phone_number(phone)
        return phone


class RequestPasswordResetForm(forms.Form):
    username = forms.CharField(required=True)
    domain = forms.CharField(required=False)
    phone = forms.CharField(required=False)

    def clean_phone(self):
        phone = self.cleaned_data.get("phone")
        if phone:
            return normalize_phone_number(phone)
        return phone


class ChangePasswordForm(forms.Form):
    old_password = forms.CharField(required=True)
    new_password = forms.CharField(required=True)
    confirm_new_password = forms.CharField(required=True)


class FinishPasswordResetForm(forms.Form):
    token = forms.CharField(required=True)
    new_password = forms.CharField(required=True)


class PatientSignupForm(forms.Form):
    """Simple signup form for patient users."""

    email = forms.EmailField(required=True, help_text="Your email address")
    password = forms.CharField(
        required=True,
        widget=forms.PasswordInput,
        help_text="Choose a strong password",
    )
    confirm_password = forms.CharField(
        required=True, widget=forms.PasswordInput, help_text="Confirm your password"
    )
    first_name = forms.CharField(required=False, help_text="Optional")
    last_name = forms.CharField(required=False, help_text="Optional")

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("password")
        confirm_password = cleaned_data.get("confirm_password")

        if password and confirm_password and password != confirm_password:
            raise forms.ValidationError("Passwords do not match")

        return cleaned_data

    def clean_email(self):
        email = self.cleaned_data.get("email")
        from django.contrib.auth import get_user_model

        User = get_user_model()
        if User.objects.filter(email=email).exists():
            raise forms.ValidationError(
                "An account with this email already exists. Please log in instead."
            )
        return email
