from django import forms

from drf_braces.serializers.form_serializer import (
    FormSerializer,
)
from .auth_forms import DomainAuthenticationForm, TOTPForm, PasswordResetForm
from rest_framework import serializers
from django.contrib.auth import get_user_model
from fhi_users.models import *

User = get_user_model()


class DomainAuthenticationFormSerializer(FormSerializer):
    class Meta(object):
        form = DomainAuthenticationForm


class TOTPFormSerializer(FormSerializer):
    class Meta(object):
        form = TOTPForm


class PasswordResetFormSerializer(FormSerializer):
    class Meta(object):
        form = PasswordResetForm


class DomainAuthResponse(serializers.Serializer):
    success = serializers.BooleanField()
    error_description = serializers.CharField()
    totp_info = serializers.CharField()
    user_type = serializers.ChoiceField(choices=["provider", "admin", "patient"])


class TOTPResponse(serializers.Serializer):
    success = serializers.BooleanField()
    error_description = serializers.CharField()
    totp_info = serializers.CharField()


class UserSignupSerializer(serializers.ModelSerializer):
    domain_name = serializers.CharField()

    class Meta(object):
        model = User
        fields = [
            "username",
            "first_name",
            "last_name",
            "password",
            "email",
            "domain_name",
        ]


class UserDomainSerializer(serializers.ModelSerializer):
    class Meta(object):
        model = UserDomain
        exclude = ("id",)


class ProfessionalSignupSerializer(serializers.ModelSerializer):
    user_signup_info = UserSignupSerializer()
    make_new_domain = serializers.BooleanField()
    user_domain = UserDomainSerializer()

    class Meta(object):
        model = ProfessionalUser
        fields = ["npi_number", "make_new_domain", "user_signup_info", "user_domain"]
