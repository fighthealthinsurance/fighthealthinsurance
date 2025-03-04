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


class WhoAmiSerializer(serializers.Serializer):
    """
    Return the current user.
    """

    email = serializers.CharField()
    domain_name = serializers.CharField()
    patient = serializers.BooleanField()
    professional = serializers.BooleanField()
    current_professional_id = serializers.IntegerField(required=False)
    highest_role = serializers.ChoiceField(
        choices=[(role.value, role.name) for role in UserRole],
        help_text="The highest permission level role of the user: none, patient, professional, or admin",
    )
    admin = serializers.BooleanField()


class UserSignupSerializer(serializers.Serializer):
    """
    Base serializer for user sign-up fields, intended to be extended.
    """

    domain_name = serializers.CharField(required=False, allow_blank=True)
    visible_phone_number = serializers.CharField(required=True)
    continue_url = serializers.CharField()  # URL to send user to post signup / payment
    username = serializers.CharField(required=True)
    first_name = serializers.CharField(required=True)
    last_name = serializers.CharField(required=True)
    password = serializers.CharField(required=True, write_only=True)
    email = serializers.EmailField(required=True)

    def validate_password(self, value):
        if len(value) < 8:
            raise serializers.ValidationError("Password must be at least 8 characters.")
        return value

    def save(self, **kwargs: Any):
        raise Exception(
            "This serializer should not be used directly -- use Patient or Professional version"
        )


class UserSerializer(serializers.ModelSerializer):
    """
    Serializer for the base django user model
    """

    class Meta(object):
        model = User
        include = ("first_name", "last_name", "email", "username", "is_active")


class UserDomainSerializer(serializers.ModelSerializer):
    """
    Serializer for domain information, excluding sensitive fields.
    """

    class Meta(object):
        model = UserDomain
        exclude = ("id", "stripe_subscription_id", "active", "professionals")


class ProfessionalSignupSerializer(serializers.ModelSerializer):
    """
    Collects professional user and optional domain creation data on sign-up.
    """

    user_signup_info = UserSignupSerializer()
    make_new_domain = serializers.BooleanField()
    skip_stripe = serializers.BooleanField(default=False)
    # If they're joining an existing domain user_domain *MUST NOT BE POPULATED*
    user_domain = UserDomainSerializer(required=False)
    npi_number = serializers.CharField(required=False, allow_blank=True)

    class Meta(object):
        model = ProfessionalUser
        fields = [
            "npi_number",
            "make_new_domain",
            "user_signup_info",
            "user_domain",
            "skip_stripe",
            "provider_type",
        ]

    def validate_npi_number(self, value):
        # Only validate if a value is provided
        if value and not re.match(r"^\d{10}$", str(value)):
            raise serializers.ValidationError("Invalid NPI number format.")
        return value


class FullProfessionalSerializer(serializers.ModelSerializer):
    """
    Serialize all of the professional
    """

    fullname = serializers.SerializerMethodField()

    class Meta(object):
        model = ProfessionalUser
        exclude: list[str] = []

    def get_fullname(self, obj):
        user: User = obj.user
        return f"{user.first_name} {user.last_name}"


class ProfessionalSignupResponseSerializer(serializers.Serializer):
    """
    Returns a 'next_url' guiding the user to checkout or follow-up steps.
    """

    next_url = serializers.URLField()


class AcceptProfessionalUserSerializer(serializers.Serializer):
    """
    Needed for accepting (or rejecting) professional users into a domain.
    """

    professional_user_id = serializers.IntegerField()
    domain_id = serializers.CharField()


class ProfessionalSummary(serializers.Serializer):
    """
    Serializer for professional user summary.
    """

    professional_user_id = serializers.IntegerField(read_only=True)
    npi = serializers.CharField(read_only=True)
    name = serializers.CharField(read_only=True)

    def to_representation(self, instance):
        # instance is a ProfessionalUser
        user: User = instance.user
        return {
            "professional_user_id": instance.id,
            "npi": instance.npi_number if instance.npi_number else "",
            "name": f"{user.first_name} {user.last_name}",
        }


class VerificationTokenSerializer(serializers.Serializer):
    """
    Verifies email activation or password reset tokens for a specific user.
    """

    token = serializers.CharField()
    user_id = serializers.IntegerField()


class GetOrCreatePendingPatientSerializer(serializers.Serializer):
    """
    Handles pending patient user creation
    """

    username = serializers.CharField(required=False, allow_blank=True)
    first_name = serializers.CharField()
    last_name = serializers.CharField()


class PatientReferenceSerializer(serializers.Serializer):
    """
    Return the patient id
    """

    id = serializers.IntegerField()


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


class StatusResponseSerializer(serializers.Serializer):
    status = serializers.CharField()
    message = serializers.CharField(required=False, allow_blank=True)
