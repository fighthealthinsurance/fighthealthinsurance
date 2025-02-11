from drf_braces.serializers.form_serializer import (
    FormSerializer,
)
from .auth_forms import LoginForm, TOTPForm, PasswordResetForm
from rest_framework import serializers
from django.contrib.auth import get_user_model
from fhi_users.models import *
from fhi_users.auth.auth_utils import (
    combine_domain_and_username,
    create_user,
    resolve_domain_id,
)
from typing import Optional

User = get_user_model()


class LoginFormSerializer(FormSerializer):
    class Meta(object):
        form = LoginForm


class TOTPFormSerializer(FormSerializer):
    class Meta(object):
        form = TOTPForm


class PasswordResetFormSerializer(FormSerializer):
    class Meta(object):
        form = PasswordResetForm


class DomainAuthResponse(serializers.Serializer):
    totp_info = serializers.CharField()
    user_type = serializers.ChoiceField(choices=["provider", "admin", "patient"])


class TOTPResponse(serializers.Serializer):
    success = serializers.BooleanField()
    error_description = serializers.CharField()
    totp_info = serializers.CharField()


class UserSignupSerializer(serializers.ModelSerializer):
    domain_name = serializers.CharField()
    phone_number = serializers.CharField()
    continue_url = serializers.CharField()  # URL to send user to post signup / payment

    class Meta(object):
        model = User
        fields = [
            # Ones from the django model
            "username",
            "first_name",
            "last_name",
            "password",
            "email",
            # Our own internal fields
            "domain_name",
            "phone_number",
            "continue_url",
        ]

    def create(self, validated_data):
        raise Exception(
            "This serializer should not be used directly -- use Patient or Professional version"
        )


class UserDomainSerializer(serializers.ModelSerializer):
    class Meta(object):
        model = UserDomain
        exclude = ("id", "stripe_subscription_id", "active")


class ProfessionalSignupSerializer(serializers.ModelSerializer):
    user_signup_info = UserSignupSerializer()
    make_new_domain = serializers.BooleanField()
    # If they're joining an existing domain
    user_domain = UserDomainSerializer(required=False)

    class Meta(object):
        model = ProfessionalUser
        fields = ["npi_number", "make_new_domain", "user_signup_info", "user_domain"]


class ProfessionalSignupResponseSerializer(serializers.Serializer):
    next_url = serializers.URLField()


class AcceptProfessionalUserSerializer(serializers.Serializer):
    professional_user_id = serializers.IntegerField()
    domain_id = serializers.CharField()


class VerificationTokenSerializer(serializers.Serializer):
    token = serializers.CharField()
    user_id = serializers.IntegerField()


class CreatePatientUserSerializer(serializers.ModelSerializer):
    country = serializers.CharField(default="USA")
    state = serializers.CharField()
    city = serializers.CharField()
    address1 = serializers.CharField()
    address2 = serializers.CharField(required=False, allow_blank=True)
    zipcode = serializers.CharField()
    domain_name = serializers.CharField(required=False, allow_blank=True)
    provider_phone_number = serializers.CharField(required=False, allow_blank=True)
    patient_phone_number = serializers.CharField(required=False, allow_blank=True)

    class Meta:
        model = User
        fields = [
            "first_name",
            "last_name",
            "username",
            "password",
            "email",
            "provider_phone_number",
            "patient_phone_number",
            "country",
            "state",
            "city",
            "address1",
            "address2",
            "zipcode",
            "domain_name",
        ]
        extra_kwargs = {"password": {"write_only": True}}

    def create(self, validated_data):
        domain_name: Optional[str] = None
        provider_phone_number: Optional[str] = None
        patient_phone_number: Optional[str] = None
        if "domain_name" in validated_data:
            domain_name = validated_data.pop("domain_name")
        if "provider_phone_number" in validated_data:
            provider_phone_number = validated_data.pop("provider_phone_number")
        if "patient_phone_number" in validated_data:
            patient_phone_number = validated_data.pop("patient_phone_number")
        user = create_user(
            email=validated_data["email"],
            raw_username=validated_data["username"],
            first_name=validated_data.get("firstname", ""),
            last_name=validated_data.get("lastname", ""),
            domain_name=domain_name,
            phone_number=provider_phone_number,
            password=validated_data["password"],
        )

        UserContactInfo.objects.create(
            user=user,
            phone_number=patient_phone_number,
            country=validated_data["country"],
            state=validated_data["state"],
            city=validated_data["city"],
            address1=validated_data["address1"],
            address2=validated_data.get("address2", ""),
            zipcode=validated_data["zipcode"],
        )

        PatientUser.objects.create(user=user, active=False)

        extra_user_properties = ExtraUserProperties.objects.create(
            user=user, email_verified=False
        )

        return user
