from drf_braces.serializers.form_serializer import (
    FormSerializer,
)
from .auth_forms import (
    LoginForm,
    TOTPForm,
    FinishPasswordResetForm,
    RequestPasswordResetForm,
)
from rest_framework import serializers
from django.contrib.auth import get_user_model
from fhi_users.models import *
from fhi_users.auth.auth_utils import (
    combine_domain_and_username,
    create_user,
    resolve_domain_id,
)
from typing import Any, Optional
import re

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


class LoginFormSerializer(FormSerializer):
    """
    Handles login form data for user authentication.
    """

    class Meta(object):
        form = LoginForm


class TOTPFormSerializer(FormSerializer):
    class Meta(object):
        form = TOTPForm


class RequestPasswordResetFormSerializer(FormSerializer):
    """
    Password reset requests.
    """

    class Meta(object):
        form = RequestPasswordResetForm


class FinishPasswordResetFormSerializer(FormSerializer):
    """
    Finishes reset requests.
    """

    class Meta(object):
        form = FinishPasswordResetForm


class TOTPResponse(serializers.Serializer):
    """
    Used to return TOTP login success status and errors to the client.
    """

    success = serializers.BooleanField()
    error_description = serializers.CharField()
    totp_info = serializers.CharField()


class UserSignupSerializer(serializers.ModelSerializer):
    """
    Base serializer for user sign-up fields, intended to be extended.
    """

    domain_name = serializers.CharField(required=False)
    visible_phone_number = serializers.CharField(required=True)
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
            "visible_phone_number",
            "continue_url",
        ]

    def validate_password(self, value):
        if len(value) < 8:
            raise serializers.ValidationError("Password must be at least 8 characters.")
        return value

    def save(self, **kwargs: Any):
        raise Exception(
            "This serializer should not be used directly -- use Patient or Professional version"
        )


class UserDomainSerializer(serializers.ModelSerializer):
    """
    Serializer for domain information, excluding sensitive fields.
    """

    class Meta(object):
        model = UserDomain
        exclude = ("id", "stripe_subscription_id", "active")


class ProfessionalSignupSerializer(serializers.ModelSerializer):
    """
    Collects professional user and optional domain creation data on sign-up.
    """

    user_signup_info = UserSignupSerializer()
    make_new_domain = serializers.BooleanField()
    # If they're joining an existing domain user_domain *MUST NOT BE POPULATED*
    user_domain = UserDomainSerializer(required=False)
    npi_number = serializers.CharField(required=False, allow_blank=True)

    class Meta(object):
        model = ProfessionalUser
        fields = ["npi_number", "make_new_domain", "user_signup_info", "user_domain"]

    def validate_npi_number(self, value):
        # Only validate if a value is provided
        if value and not re.match(r"^\d{10}$", str(value)):
            raise serializers.ValidationError("Invalid NPI number format.")
        return value


class ProfessionalSignupResponseSerializer(serializers.Serializer):
    """
    Returns a 'next_url' guiding the user to checkout or follow-up steps.
    """

    next_url = serializers.URLField()


class AcceptProfessionalUserSerializer(serializers.Serializer):
    """
    Needed for accepting professional users into a domain.
    """

    professional_user_id = serializers.IntegerField()
    domain_id = serializers.CharField()


class VerificationTokenSerializer(serializers.Serializer):
    """
    Verifies email activation or password reset tokens for a specific user.
    """

    token = serializers.CharField()
    user_id = serializers.IntegerField()


class CreatePatientUserSerializer(serializers.ModelSerializer):
    """
    Handles patient user creation, including address/contact details.
    """

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
