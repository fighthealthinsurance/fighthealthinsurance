from django import forms


class LoginForm(forms.Form):
    username = forms.CharField(required=True)
    password = forms.CharField(required=True)
    # We need one of the domain or phone to be not null
    domain = forms.CharField(required=False)
    phone = forms.CharField(required=False)


class TOTPForm(forms.Form):
    username = forms.CharField(required=True)
    # We need one of the domain or phone to be not null
    domain = forms.CharField(required=False)
    phone = forms.CharField(required=False)
    totp = forms.CharField(required=True)


class PasswordResetForm(forms.Form):
    username = forms.CharField(required=True)
    domain = forms.CharField(required=True)
