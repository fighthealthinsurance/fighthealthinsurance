from django import forms

from drf_braces.serializers.form_serializer import (
    FormSerializer,
)
from .auth_forms import DomainAuthenticationForm, TOTPForm, PasswordResetForm
from rest_framework import serializers


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


class TOTPResponse(serializers.Serializer):
    success = serializers.BooleanField()
    error_description = serializers.CharField()
    totp_info = serializers.CharField()
