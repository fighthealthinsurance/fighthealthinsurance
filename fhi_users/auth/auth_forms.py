from django import forms


class DomainAuthenticationForm(forms.Form):
    username = forms.CharField(required=True)
    password = forms.CharField(required=True)
    domain = forms.CharField(required=True)


class TOTPForm(forms.Form):
    username = forms.CharField(required=True)
    domain = forms.CharField(required=True)
    totp = forms.CharField(required=True)


class PasswordResetForm(forms.Form):
    username = forms.CharField(required=True)
    domain = forms.CharField(required=True)
