from loguru import logger
from typing import Any, Optional, TYPE_CHECKING

from rest_framework import status
from rest_framework import viewsets
from rest_framework.views import APIView
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework import permissions
from rest_framework.viewsets import ViewSet
from rest_framework.mixins import CreateModelMixin, ListModelMixin
from rest_framework.viewsets import GenericViewSet
from rest_framework.permissions import IsAuthenticated

from django.http import HttpRequest
from django.conf import settings
from django.urls import reverse
from django.contrib.auth import get_user_model
from django.contrib.auth import authenticate, login
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.utils.encoding import force_bytes, force_str
from django.utils import timezone
from django.template.loader import render_to_string
from django.contrib.sites.shortcuts import get_current_site
from django.contrib.auth.tokens import default_token_generator
from django.shortcuts import get_object_or_404


import stripe

from fhi_users.models import (
    UserDomain,
    PatientUser,
    ProfessionalUser,
    ProfessionalDomainRelation,
    VerificationToken,
    ExtraUserProperties,
    ResetToken,
)
from fhi_users.auth import rest_serializers as serializers
from fhi_users.auth.auth_utils import (
    create_user,
    combine_domain_and_username,
    user_is_admin_in_domain,
    resolve_domain_id,
    get_patient_or_create_pending_patient,
)
from fighthealthinsurance.rest_mixins import CreateMixin, SerializerMixin
from rest_framework.serializers import Serializer
from fighthealthinsurance import stripe_utils
from fhi_users.emails import send_verification_email, send_password_reset_email

if TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


class WhoAmIViewSet(viewsets.ViewSet):
    def list(self, request: Request):
        user: User = request.user  # type: ignore
        if user.is_authenticated:
            # Get the user domain from the session
            domain_id = request.session.get("domain_id")
            if not domain_id:
                return Response(
                    {"error": "Domain ID not found in session"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            user_domain = UserDomain.objects.get(id=domain_id)
            patient = False
            professional = False
            professional_id: Optional[int] = None
            admin = False
            try:
                PatientUser.objects.get(user=user, active=True)
                patient = True
            except PatientUser.DoesNotExist:
                pass
            try:
                _professional = ProfessionalUser.objects.get(user=user, active=True)
                professional = True
                professional_id = _professional.id
                try:
                    ProfessionalDomainRelation.objects.get(
                        professional=_professional,
                        domain=user_domain,
                        active=True,
                        admin=True,
                    )
                    admin = True
                except ProfessionalDomainRelation.DoesNotExist:
                    pass
            except ProfessionalUser.DoesNotExist:
                pass
            return Response(
                serializers.WhoAmiSerializer(
                    {
                        "email": user.email,
                        "domain_name": user_domain.name,
                        "patient": patient,
                        "professional": professional,
                        "current_professional_id": professional_id,
                        "admin": admin,
                    }
                ).data,
                status=status.HTTP_200_OK,
            )
        else:
            return Response(
                {"error": "User is not authenticated"},
                status=status.HTTP_401_UNAUTHORIZED,
            )


class ProfessionalUserViewSet(viewsets.ViewSet, CreateMixin):
    """
    Handles professional user sign-up and domain acceptance or rejection.
    """

    def get_serializer_class(self):
        if self.action == "accept":
            return serializers.AcceptProfessionalUserSerializer
        else:
            return serializers.ProfessionalSignupSerializer

    def get_permissions(self):
        """
        Different permissions for different actions
        """
        permission_classes = []  # type: ignore
        if self.action == "list":
            permission_classes = []
        elif self.action == "accept" or self.action == "reject":
            permission_classes = [IsAuthenticated]
        else:
            permission_classes = []
        return [permission() for permission in permission_classes]

    @action(detail=False, methods=["post"])
    def list_active_in_domain(self, request) -> Response:
        domain_id = request.session["domain_id"]
        domain = UserDomain.objects.get(id=domain_id)
        # Ensure current user is an active professional in domain
        current_user: User = request.user
        professional_user = ProfessionalUser.objects.get(user=current_user)
        get_object_or_404(
            ProfessionalDomainRelation.objects.filter(
                professional=ProfessionalUser.objects.get(user=current_user),
                domain=domain,
                active=True,
            )
        )
        professionals = domain.get_professional_users(active=True)
        serializer = serializers.ProfessionalSummary(professionals, many=True)
        return Response({"active_professionals": serializer.data})

    @action(detail=False, methods=["post"])
    def list_pending_in_domain(self, request) -> Response:
        domain_id = request.session["domain_id"]
        domain = UserDomain.objects.get(id=domain_id)
        # Ensure current user is active in domain
        current_user: User = request.user
        get_object_or_404(
            ProfessionalDomainRelation.objects.filter(
                professional=ProfessionalUser.objects.get(user=current_user),
                domain=domain,
                active=True,
            )
        )
        professionals = domain.get_professional_users(pending=True)
        serializer = serializers.ProfessionalSummary(professionals, many=True)
        return Response({"pending_professionals": serializer.data})

    @action(detail=False, methods=["post"])
    def reject(self, request) -> Response:
        """
        Rejects a professional user from a domain.
        """
        serializer = self.deserialize(data=request.data)
        serializer.is_valid(raise_exception=True)
        professional_user_id: int = serializer.validated_data["professional_user_id"]
        domain_id = serializer.validated_data["domain_id"]
        current_user: User = request.user
        current_user_admin_in_domain = user_is_admin_in_domain(current_user, domain_id)
        if not current_user_admin_in_domain:
            # Credentials are valid but does not have permissions
            return Response(status=status.HTTP_403_FORBIDDEN)
        relation = ProfessionalDomainRelation.objects.get(
            professional_id=professional_user_id, pending=True, domain_id=domain_id
        )
        relation.pending = False
        # TODO: Add to model
        relation.rejected = True
        relation.active = False
        relation.save()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=["post"])
    def accept(self, request) -> Response:
        """
        Accepts a professional user into a domain, optionally updating subscriptions.
        """
        serializer = self.deserialize(data=request.data)
        serializer.is_valid(raise_exception=True)
        professional_user_id: int = serializer.validated_data["professional_user_id"]
        domain_id = serializer.validated_data["domain_id"]
        try:
            current_user: User = request.user  # type: ignore
            current_user_admin_in_domain = user_is_admin_in_domain(
                current_user, domain_id
            )
            if not current_user_admin_in_domain:
                # Credentials are valid but does not have permissions
                return Response(status=status.HTTP_403_FORBIDDEN)
        except Exception as e:
            # Unexecpted generic error, fail closed
            logger.opt(exception=e).error("Error in accepting professional user")
            return Response(status=status.HTTP_401_UNAUTHORIZED)
        try:
            relation = ProfessionalDomainRelation.objects.get(
                professional_id=professional_user_id, pending=True, domain_id=domain_id
            )
            professional_user = ProfessionalUser.objects.get(id=professional_user_id)
            professional_user.active = True
            professional_user.save()
            relation.pending = False
            relation.save()

            stripe.api_key = settings.STRIPE_API_SECRET_KEY
            if relation.domain.stripe_subscription_id:
                subscription = stripe.Subscription.retrieve(
                    relation.domain.stripe_subscription_id
                )
                stripe.Subscription.modify(
                    subscription.id,
                    items=[
                        {
                            "id": subscription["items"]["data"][0].id,
                            "quantity": subscription["items"]["data"][0].quantity + 1,
                        }
                    ],
                )
            else:
                logger.debug("Skipping no subscription present.")

            return Response({"status": "accepted"}, status=status.HTTP_200_OK)
        except ProfessionalDomainRelation.DoesNotExist:
            return Response(
                {"error": "Relation not found or already accepted"},
                status=status.HTTP_404_NOT_FOUND,
            )

    def perform_create(
        self, request: HttpRequest, serializer: Serializer
    ) -> Response | serializers.ProfessionalSignupResponseSerializer:
        data: dict[str, bool | str | dict[str, str]] = serializer.validated_data  # type: ignore
        user_signup_info: dict[str, str] = data["user_signup_info"]  # type: ignore
        domain_name: Optional[str] = user_signup_info["domain_name"]  # type: ignore
        visible_phone_number: Optional[str] = user_signup_info["visible_phone_number"]  # type: ignore
        new_domain: bool = bool(data["make_new_domain"])  # type: ignore

        if not new_domain:
            # In practice the serializer may enforce these for us
            try:
                UserDomain.objects.filter(name=domain_name).get()
            except UserDomain.DoesNotExist:
                try:
                    UserDomain.objects.get(visible_phone_number=visible_phone_number)
                except:
                    return Response(
                        {"error": "Domain does not exist"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
        else:
            if UserDomain.objects.filter(name=domain_name).count() != 0:
                return Response(
                    {"error": "Domain already exists"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if (
                UserDomain.objects.filter(
                    visible_phone_number=visible_phone_number
                ).count()
                != 0
            ):
                return Response(
                    {"error": "Visible phone number already exists"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if "user_domain" not in data:
                return Response(
                    {
                        "error": "Need domain info when making a new domain or solo provider"
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            user_domain_info: dict[str, str] = data["user_domain"]  # type: ignore
            if domain_name != user_domain_info["name"]:
                if user_domain_info["name"] is None:
                    user_domain_info["name"] = domain_name
                else:
                    return Response(
                        {"error": "Domain name and user domain name must match"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            if visible_phone_number != user_domain_info["visible_phone_number"]:
                if user_domain_info["visible_phone_number"] is None:
                    user_domain_info["visible_phone_number"] = visible_phone_number
                else:
                    return Response(
                        {
                            "error": "Visible phone number and user domain visible phone number must match"
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            UserDomain.objects.create(
                active=False,
                **user_domain_info,
            )

        user_domain: UserDomain = UserDomain.objects.get(name=domain_name)
        raw_username: str = user_signup_info["username"]  # type: ignore
        email: str = user_signup_info["email"]  # type: ignore
        password: str = user_signup_info["password"]  # type: ignore
        first_name: str = user_signup_info["first_name"]  # type: ignore
        last_name: str = user_signup_info["last_name"]  # type: ignore
        user = create_user(
            raw_username=raw_username,
            domain_name=domain_name,
            phone_number=visible_phone_number,
            email=email,
            password=password,
            first_name=first_name,
            last_name=last_name,
        )
        send_verification_email(request, user)
        professional_user = ProfessionalUser.objects.create(
            user=user,
            active=False,
        )
        ProfessionalDomainRelation.objects.create(
            professional=professional_user,
            domain=user_domain,
            active=False,
            admin=new_domain,
            pending=True,
        )

        if settings.DEBUG and data["skip_stripe"]:
            product_id, price_id = stripe_utils.get_or_create_price(
                "Basic Professional Subscription", 2500, recurring=True
            )

            checkout_session = stripe.checkout.Session.create(
                payment_method_types=["card"],
                line_items=[{"price": price_id, "quantity": 1}],
                mode="subscription",
                success_url=user_signup_info["continue_url"],
                cancel_url=user_signup_info["continue_url"],
                customer_email=email,
                metadata={
                    "payment_type": "professional_domain_subscription",
                    "professional_id": str(professional_user.id),
                    "domain_id": str(user_domain.id),
                },
            )
            extra_user_properties = ExtraUserProperties.objects.create(
                user=user, email_verified=False
            )
            subscription_id = checkout_session.subscription
            return serializers.ProfessionalSignupResponseSerializer(
                {"next_url": checkout_session.url}
            )
        else:
            return serializers.ProfessionalSignupResponseSerializer(
                {"next_url": "https://www.fightpaperwork.com/?q=testmode"}
            )


class RestLoginView(ViewSet, SerializerMixin):
    serializer_class = serializers.LoginFormSerializer

    @action(detail=False, methods=["post"])
    def login(self, request: Request) -> Response:
        serializer = self.deserialize(request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        raw_username: str = data.get("username")
        password: str = data.get("password")
        domain: str = data.get("domain")
        phone: str = data.get("phone")
        try:
            domain_id = resolve_domain_id(domain_name=domain, phone_number=phone)
            username = combine_domain_and_username(
                raw_username, phone_number=phone, domain_id=domain_id
            )
        except Exception as e:
            return Response(
                {
                    "status": "failure",
                    "message": f"Domain or phone number not found -- {e}",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        user = authenticate(username=username, password=password)
        if user:
            request.session["domain_id"] = domain_id
            logger.info(
                f"User {user.username} logged in setting domain id to {domain_id}"
            )
            login(request, user)
            return Response({"status": "success"})
        try:
            user = User.objects.get(username=username)
            if not user.is_active:
                return Response(
                    {
                        "status": "failure",
                        "message": "User is inactive -- please verify your e-mail",
                    },
                    status=status.HTTP_401_UNAUTHORIZED,
                )
        except User.DoesNotExist:
            pass
        return Response(
            {"status": "failure", "message": f"Invalid credentials"},
            status=status.HTTP_401_UNAUTHORIZED,
        )


class PatientUserViewSet(ViewSet, CreateMixin):
    """Create a new patient user."""

    def get_serializer_class(self):
        if self.action == "create":
            return serializers.CreatePatientUserSerializer
        else:
            return serializers.GetOrCreatePendingPatientSerializer

    def perform_create(self, request: Request, serializer) -> Response:
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        send_verification_email(request, user)
        return Response({"status": "pending"})

    @action(detail=False, methods=["post", "options"])
    def get_or_create_pending(self, request: Request) -> Response:
        print(f"Called...")
        serializer = self.deserialize(data=request.data)
        serializer.is_valid(raise_exception=True)
        domain = UserDomain.objects.get(id=request.session["domain_id"])
        user = get_patient_or_create_pending_patient(
            email=serializer.validated_data["username"],
            raw_username=serializer.validated_data["username"],
            domain=domain,
            fname=serializer.validated_data["first_name"],
            lname=serializer.validated_data["last_name"],
        )
        print(f"User is {user}")
        response_serializer = serializers.PatientReferenceSerializer({"id": user.id})
        return Response(response_serializer.data)


class VerifyEmailViewSet(ViewSet, SerializerMixin):
    """
    Handles email verification and resending verification emails.
    """

    serializer_class = serializers.VerificationTokenSerializer

    @action(detail=False, methods=["post"])
    def verify(self, request: Request) -> Response:
        serializer = self.deserialize(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            uid = serializer.validated_data["user_id"]
            user = User.objects.get(pk=uid)
        except (TypeError, ValueError, OverflowError, User.DoesNotExist) as e:
            return Response(
                {
                    "status": "failure",
                    "message": "Invalid activation link [user not found]",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        token = serializer.validated_data["token"]
        try:
            verification_token = VerificationToken.objects.get(user=user, token=token)
            if timezone.now() > verification_token.expires_at:
                return Response(
                    {"status": "failure", "message": "Activation link has expired"},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )
            user.is_active = True
            user.save()
            try:
                extraproperties = ExtraUserProperties.objects.get(user=user)
            except:
                extraproperties = ExtraUserProperties.objects.create(user=user)
            extraproperties.email_verified = True
            extraproperties.save()
            verification_token.delete()
            try:
                PatientUser.objects.filter(user=user).update(is_active=True)
            except:
                pass
            try:
                ProfessionalUser.objects.filter(user=user).update(is_active=True)
            except:
                pass
            return Response({"status": "success"})
        except VerificationToken.DoesNotExist as e:
            return Response(
                {"status": "failure", "message": "Invalid activation link"},
                status=status.HTTP_400_BAD_REQUEST,
            )

    @action(detail=False, methods=["post"])
    def resend(self, request: Request) -> Response:
        """
        Resends verification email for unverified users.
        """
        serializer = self.deserialize(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]
        send_verification_email(request, user)
        return Response({"status": "verification email resent"})


class PasswordResetViewSet(ViewSet, SerializerMixin):
    """
    Handles password reset requests and completion.
    """

    def get_serializer_class(self):
        if self.action == "finish_reset":
            return serializers.FinishPasswordResetFormSerializer
        return serializers.RequestPasswordResetFormSerializer

    @action(detail=False, methods=["post"])
    def request_reset(self, request: Request) -> Response:
        """Request a password reset."""
        serializer = self.deserialize(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            # Find user by domain/phone
            domain_id = resolve_domain_id(
                domain_name=data.get("domain"), phone_number=data.get("phone")
            )
            username = combine_domain_and_username(
                data["username"], phone_number=data.get("phone"), domain_id=domain_id
            )
            user = User.objects.get(username=username)

            # Delete any existing reset tokens
            ResetToken.objects.filter(user=user).delete()

            # Create new reset token
            reset_token = ResetToken.objects.create(user=user)

            # Send reset email
            send_password_reset_email(user.email, reset_token.token)

            return Response({"status": "reset_requested"})

        except Exception as e:
            logger.error(f"Password reset request failed: {e}")
            return Response(
                {"status": "failure", "message": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )

    @action(detail=False, methods=["post"])
    def finish_reset(self, request: Request) -> Response:
        """Complete a password reset."""
        serializer = self.deserialize(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            reset_token = ResetToken.objects.get(token=data["token"])

            # Check if token has expired
            if timezone.now() > reset_token.expires_at:
                reset_token.delete()
                return Response(
                    {"status": "failure", "message": "Reset token has expired"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Update password
            user = reset_token.user
            user.set_password(data["new_password"])
            user.save()

            # Clean up token
            reset_token.delete()

            return Response({"status": "password_reset_complete"})

        except ResetToken.DoesNotExist:
            return Response(
                {"status": "failure", "message": "Invalid reset token"},
                status=status.HTTP_400_BAD_REQUEST,
            )
