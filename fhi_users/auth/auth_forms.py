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


class EmailOnlyLoginForm(forms.Form):
    """Email-only login form for the new authentication system."""

    email = forms.EmailField(required=True)
    password = forms.CharField(required=True)
