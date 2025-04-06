from loguru import logger
from typing import Optional, TYPE_CHECKING
import time
import json
import uuid

from rest_framework import status
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import ViewSet
from rest_framework.permissions import IsAuthenticated

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth import authenticate, login
from django.utils import timezone
from django.shortcuts import get_object_or_404
from django.views.decorators.cache import cache_control
from django.views.decorators.vary import vary_on_cookie
from django.utils.decorators import method_decorator
from django.db import transaction
from django.db import utils as django_db_utils
from django.db.utils import IntegrityError


import stripe

from fhi_users.models import (
    UserDomain,
    PatientUser,
    ProfessionalUser,
    ProfessionalDomainRelation,
    VerificationToken,
    ExtraUserProperties,
    ResetToken,
    UserRole,
    UserContactInfo,
)
from fighthealthinsurance.models import StripeRecoveryInfo
from fhi_users.auth import rest_serializers as serializers
from fighthealthinsurance import rest_serializers as common_serializers
from fhi_users.auth import auth_utils
from fhi_users.auth.auth_utils import (
    create_user,
    combine_domain_and_username,
    user_is_admin_in_domain,
    resolve_domain_id,
    get_patient_or_create_pending_patient,
    get_next_fake_username,
    validate_password,
)
from fighthealthinsurance.rest_mixins import CreateMixin, SerializerMixin
from rest_framework.serializers import Serializer
from fighthealthinsurance import stripe_utils
from fhi_users.emails import (
    send_verification_email,
    send_password_reset_email,
    send_professional_created_email,
)

from drf_spectacular.utils import extend_schema

if TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


class UserDomainExistsViewSet(viewsets.ViewSet, SerializerMixin):
    """
    Check if a UserDomain exists by name or phone number.
    """

    def get_serializer_class(self):
        return serializers.DomainExistsSerializer

    @extend_schema(
        responses={
            200: serializers.DomainExistsResponseSerializer,
            400: common_serializers.ErrorSerializer,
        }
    )
    @action(detail=False, methods=["post"])
    def check(self, request: Request) -> Response:
        """
        Check if a domain exists by name or phone number.
        """
        serializer = self.deserialize(data=request.data)
        serializer.is_valid(raise_exception=True)

        domain_name = serializer.validated_data.get("domain_name")
        phone_number = serializer.validated_data.get("phone_number")

        if not domain_name and not phone_number:
            return Response(
                common_serializers.ErrorSerializer(
                    {"error": "Either domain_name or phone_number must be provided"}
                ).data,
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Check if domain exists
        exists = False
        if domain_name:
            exists = UserDomain.objects.filter(name=domain_name).exists()

        if not exists and phone_number:
            exists = UserDomain.objects.filter(
                visible_phone_number=phone_number
            ).exists()

        return Response(
            serializers.DomainExistsResponseSerializer({"exists": exists}).data,
            status=status.HTTP_200_OK,
        )


class WhoAmIViewSet(viewsets.ViewSet):
    """
    Returns the current user's information, including their roles and domain.
    """

    @method_decorator(
        cache_control(max_age=600, private=True)
    )  # Cache for 10 minutes, private to user, vary only on cookie header
    @method_decorator(vary_on_cookie)  # Vary only on the session cookie
    @extend_schema(
        responses={
            200: serializers.WhoAmiSerializer,
            400: common_serializers.ErrorSerializer,
            401: common_serializers.ErrorSerializer,
        }
    )
    def list(self, request: Request) -> Response:
        user: User = request.user  # type: ignore
        if user.is_authenticated:
            # Get the user domain from the session
            domain_id = auth_utils.get_domain_id_from_request(request)
            if not domain_id:
                return Response(
                    common_serializers.ErrorSerializer(
                        {"error": "Domain ID not found in session"}
                    ).data,
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

            # Use the UserRole enum to get the highest role
            highest_role = UserRole.get_highest_role(
                is_patient=patient, is_professional=professional, is_admin=admin
            )

            return Response(
                serializers.WhoAmiSerializer(
                    [
                        {
                            "email": user.email,
                            "domain_name": user_domain.name,
                            "patient": patient,
                            "professional": professional,
                            "current_professional_id": professional_id,
                            "highest_role": highest_role.value,
                            "domain_id": user_domain.id,
                            "admin": admin,
                            "beta": user_domain.beta,  # Add beta flag
                        }
                    ],
                    many=True,  # This is to match list endpoints returning arrays.
                ).data,
                status=status.HTTP_200_OK,
            )
        else:
            return Response(
                common_serializers.ErrorSerializer(
                    {"error": "User is not authenticated"}
                ).data,
                status=status.HTTP_401_UNAUTHORIZED,
            )


class ProfessionalUserViewSet(viewsets.ViewSet, CreateMixin):
    """
    Handles professional user sign-up and domain acceptance or rejection.
    """

    def get_serializer_class(self):
        if self.action == "accept" or self.action == "reject":
            return serializers.AcceptProfessionalUserSerializer
        elif self.action == "create":
            return serializers.ProfessionalSignupSerializer
        elif (
            self.action == "list_active_in_domain"
            or self.action == "list_pending_in_domain"
        ):
            return serializers.EmptySerializer
        elif self.action == "finish_payment":
            return serializers.FinishPaymentSerializer
        elif self.action == "invite":
            return serializers.InviteProfessionalSerializer
        elif self.action == "create_professional_in_current_domain":
            return serializers.CreateProfessionalInCurrentDomainSerializer
        else:
            return serializers.EmptySerializer

    def get_permissions(self):
        """
        Different permissions for different actions
        """
        permission_classes = []  # type: ignore
        if self.action == "list":
            permission_classes = []
        elif self.action in [
            "accept",
            "reject",
            "invite",
            "create_professional_in_current_domain",
        ]:
            permission_classes = [IsAuthenticated]
        else:
            permission_classes = []
        return [permission() for permission in permission_classes]

    @extend_schema(
        responses={
            200: serializers.StatusResponseSerializer,
            400: common_serializers.ErrorSerializer,
            403: common_serializers.ErrorSerializer,
        }
    )
    @action(detail=False, methods=["post"])
    def create_professional_in_current_domain(self, request: Request) -> Response:
        """
        Create a new professional account in the admin's domain and send them an email.
        """
        serializer = self.deserialize(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Get the current user's domain
        try:
            current_user: User = request.user  # type: ignore
            domain_id = auth_utils.get_domain_id_from_request(request)
            if not domain_id:
                logger.opt(exception=True).error(
                    f"Domain ID not found in session for user: {current_user.username}"
                )
                return Response(
                    common_serializers.ErrorSerializer(
                        {"error": "Domain ID not found in session"}
                    ).data,
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Ensure current user is an admin in the domain
            user_domain = UserDomain.objects.get(id=domain_id)
            if not user_is_admin_in_domain(current_user, domain_id):
                logger.opt(exception=True).error(
                    f"User {current_user.username} attempted to create professional without admin privileges in domain {domain_id}"
                )
                return Response(
                    common_serializers.ErrorSerializer(
                        {"error": "User does not have admin privileges"}
                    ).data,
                    status=status.HTTP_403_FORBIDDEN,
                )

            # Extract data from serializer
            email = serializer.validated_data["email"]
            first_name = serializer.validated_data["first_name"]
            last_name = serializer.validated_data["last_name"]
            npi_number = serializer.validated_data.get("npi_number", "")
            provider_type = serializer.validated_data.get("provider_type", "")

            # Check if user with this email already exists
            if User.objects.filter(email=email).exists():
                logger.opt(exception=True).error(
                    f"Cannot create professional - email already exists: {email}"
                )
                return Response(
                    common_serializers.ErrorSerializer(
                        {"error": "A user with this email already exists"}
                    ).data,
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Generate a random password (user will reset it)
            temp_password = uuid.uuid4().hex
            logger.info(
                f"Creating professional {email} in domain {user_domain.name} (ID: {domain_id})"
            )

            with transaction.atomic():
                # Create the user
                try:
                    user = create_user(
                        raw_username=email,
                        domain_name=user_domain.name,
                        phone_number=user_domain.visible_phone_number,
                        email=email,
                        password=temp_password,
                        first_name=first_name,
                        last_name=last_name,
                    )
                except Exception as e:
                    logger.error(
                        f"Failed to create user for professional: {email} in domain {user_domain.name}: {str(e)}"
                    )
                    raise

                # Create professional user record
                try:
                    professional_user = ProfessionalUser.objects.create(
                        user=user,
                        active=True,
                        npi_number=npi_number,
                        provider_type=provider_type,
                    )
                except Exception as e:
                    logger.error(
                        f"Failed to create professional user record for {email}: {str(e)}"
                    )
                    raise

                # Create relationship with domain (active, not pending)
                try:
                    ProfessionalDomainRelation.objects.create(
                        professional=professional_user,
                        domain=user_domain,
                        active=True,
                        admin=False,  # New professionals are not admins by default
                        pending=False,
                    )
                except Exception as e:
                    logger.error(
                        f"Failed to create domain relation for professional {email} with domain {user_domain.name}: {str(e)}"
                    )
                    raise

                # Create extra user properties
                try:
                    ExtraUserProperties.objects.create(
                        user=user,
                        email_verified=True,  # Auto-verify since admin is creating
                    )
                except Exception as e:
                    logger.error(
                        f"Failed to create extra user properties for professional {email}: {str(e)}"
                    )
                    raise

                # Send email notification to the professional
                context = {
                    "practice_name": user_domain.name or "our practice",
                    "inviter_name": f"{current_user.first_name} {current_user.last_name}",
                    "professional_name": f"{first_name} {last_name}",
                    "practice_phone": user_domain.visible_phone_number,
                    "email": email,
                }

                try:
                    send_professional_created_email(email, context)
                    logger.info(
                        f"Successfully sent welcome email to professional {email}"
                    )
                except Exception as e:
                    logger.error(
                        f"Failed to send welcome email to professional {email}: {str(e)}"
                    )
                    # Don't raise here, still return success even if email fails

                logger.info(
                    f"Successfully created professional user {email} in domain {user_domain.name}"
                )
                return Response(
                    serializers.StatusResponseSerializer(
                        {
                            "status": "professional_created",
                            "message": f"Professional account created for {email}",
                        }
                    ).data,
                    status=status.HTTP_200_OK,
                )

        except Exception as e:
            logger.opt(exception=True).error(
                f"Unexpected error creating professional: {str(e)}"
            )
            return Response(
                common_serializers.ErrorSerializer({"error": f"Error: {str(e)}"}).data,
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @extend_schema(
        responses={
            200: serializers.StatusResponseSerializer,
            400: common_serializers.ErrorSerializer,
        }
    )
    @action(detail=False, methods=["post"])
    def invite(self, request: Request) -> Response:
        """
        Invite a professional to join the practice by sending them an email.
        """
        serializer = self.deserialize(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Get the current user's domain
        try:
            current_user: User = request.user  # type: ignore
            domain_id = auth_utils.get_domain_id_from_request(request)
            if not domain_id:
                return Response(
                    common_serializers.ErrorSerializer(
                        {"error": "Domain ID not found in session"}
                    ).data,
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Ensure current user is an admin in the domain
            user_domain = UserDomain.objects.get(id=domain_id)
            if not user_is_admin_in_domain(current_user, domain_id):
                return Response(
                    common_serializers.ErrorSerializer(
                        {"error": "User does not have admin privileges"}
                    ).data,
                    status=status.HTTP_403_FORBIDDEN,
                )

            # Extract data from serializer
            professional_email = serializer.validated_data["user_email"]
            professional_name = serializer.validated_data.get("name", "")

            # Create context for email template
            context = {
                "practice_name": user_domain.name or "our practice",
                "practice_number": user_domain.visible_phone_number,
                "inviter_name": f"{current_user.first_name} {current_user.last_name}",
                "professional_name": professional_name,
            }

            # Send invitation email
            from fhi_users.emails import send_professional_invitation_email

            send_professional_invitation_email(professional_email, context)

            return Response(
                serializers.StatusResponseSerializer(
                    {
                        "status": "invitation_sent",
                        "message": f"Invitation sent to {professional_email}",
                    }
                ).data,
                status=status.HTTP_200_OK,
            )

        except Exception as e:
            logger.opt(exception=e).error("Error inviting professional")
            return Response(
                common_serializers.ErrorSerializer({"error": f"Error {e}"}).data,
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @method_decorator(
        cache_control(max_age=600, private=True)
    )  # Cache for 10 minutes, private to user, vary only on cookie header
    @method_decorator(vary_on_cookie)  # Vary only on the session cookie
    @extend_schema(
        responses={
            200: serializers.ProfessionalSummary(many=True),
        }
    )
    @action(detail=False, methods=["post"])
    def list_active_in_domain(self, request) -> Response:
        """List the active users in a given domain"""
        domain_id = auth_utils.get_domain_id_from_request(request)
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
        return Response(serializer.data)

    @method_decorator(
        cache_control(max_age=600, private=True)
    )  # Cache for 10 minutes, private to user, vary only on cookie header
    @method_decorator(vary_on_cookie)  # Vary only on the session cookie
    @extend_schema(
        responses={
            200: serializers.ProfessionalSummary(many=True),
        }
    )
    @action(detail=False, methods=["post"])
    def list_pending_in_domain(self, request) -> Response:
        """List the pending user in a given domain"""
        domain_id = auth_utils.get_domain_id_from_request(request)
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
        return Response(serializer.data)

    @extend_schema(
        responses={
            200: serializers.StatusResponseSerializer,
            403: common_serializers.ErrorSerializer,
        }
    )
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
            return Response(
                common_serializers.ErrorSerializer(
                    {"error": "User does not have admin privileges"}
                ).data,
                status=status.HTTP_403_FORBIDDEN,
            )
        relation = ProfessionalDomainRelation.objects.get(
            professional_id=professional_user_id, pending=True, domain_id=domain_id
        )
        relation.pending = False
        # TODO: Add to model
        relation.rejected = True
        relation.active = False
        relation.save()
        return Response(
            serializers.StatusResponseSerializer(
                {"status": "rejected", "message": "Professional user rejected"}
            ).data,
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        responses={
            200: serializers.StatusResponseSerializer,
            403: common_serializers.ErrorSerializer,
            404: common_serializers.ErrorSerializer,
            500: common_serializers.ErrorSerializer,
        }
    )
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
                return Response(
                    common_serializers.ErrorSerializer(
                        {
                            "error": "User does not have admin privileges",
                        }
                    ).data,
                    status=status.HTTP_403_FORBIDDEN,
                )
        except Exception as e:
            # Unexecpted generic error, fail closed
            logger.opt(exception=e).error("Error in accepting professional user")
            return Response(
                common_serializers.ErrorSerializer({"error": f"Error {e}"}).data,
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        try:
            relation = ProfessionalDomainRelation.objects.get(
                professional_id=professional_user_id, pending=True, domain_id=domain_id
            )
            professional_user = ProfessionalUser.objects.get(id=professional_user_id)
            professional_user.active = True
            professional_user.save()
            relation.pending = False
            relation.save()

            return Response(
                serializers.StatusResponseSerializer(
                    {"status": "accepted", "message": "Professional user accepted"}
                ).data,
                status=status.HTTP_200_OK,
            )
        except ProfessionalDomainRelation.DoesNotExist:
            return Response(
                common_serializers.ErrorSerializer(
                    {
                        "error": "Relation not found or already accepted",
                    }
                ).data,
                status=status.HTTP_404_NOT_FOUND,
            )

    @extend_schema(
        responses={
            200: serializers.ProfessionalSignupResponseSerializer,
            201: serializers.ProfessionalSignupResponseSerializer,
            400: common_serializers.ErrorSerializer,
        }
    )
    def create(self, request: Request) -> Response:
        """
        Creates a new professional user and optionally a new domain.
        """
        return super().create(request)

    def create_stripe_checkout_session(
        self,
        email,
        professional_user_id,
        user_domain,
        continue_url,
        cancel_url,
        card_required=False,
    ):
        payment_method_collection = "always"
        if not card_required:
            payment_method_collection = "if_required"
        base_product_id, base_price_id = stripe_utils.get_or_create_price(
            "FP Basic Professional Plan", 2500, recurring=True
        )
        metered_product_id, metered_price_id = stripe_utils.get_or_create_price(
            "Incremental FP Appeal", 1000, recurring=True, metered=True
        )
        line_items = [
            {"price": base_price_id, "quantity": 1},
            {"price": metered_price_id},
        ]
        stripe_recovery_info = StripeRecoveryInfo.objects.create(items=line_items)
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=line_items,  # type: ignore
            mode="subscription",
            success_url=continue_url,
            cancel_url=cancel_url,
            customer_email=email,
            allow_promotion_codes=True,
            metadata={
                "payment_type": "professional_domain_subscription",
                "professional_id": str(professional_user_id),
                "domain_id": str(user_domain.id),
                "recovery_info_id": str(stripe_recovery_info.id),
            },
            subscription_data={
                "trial_period_days": 30,
                "trial_settings": {
                    "end_behavior": {"missing_payment_method": "cancel"}
                },
            },
            payment_method_collection=payment_method_collection,  # type: ignore
            expires_at=int(time.time() + (3600 * 1)),
        )
        return checkout_session

    @extend_schema(
        responses={
            200: serializers.FinishPaymentResponseSerializer,
            400: common_serializers.ErrorSerializer,
            403: common_serializers.ErrorSerializer,
        }
    )
    @action(detail=False, methods=["get", "post"])
    def finish_payment(self, request: Request) -> Response:
        domain_id = request.data.get("domain_id") or request.query_params.get(
            "domain_id"
        )
        professional_user_id = request.data.get(
            "professional_user_id"
        ) or request.query_params.get("professional_user_id")
        domain_name = request.data.get("domain_name") or request.query_params.get(
            "domain_name"
        )
        domain_phone = request.data.get("domain_phone") or request.query_params.get(
            "domain_phone"
        )
        user_email = request.data.get("user_email") or request.query_params.get(
            "user_email"
        )
        continue_url = request.data.get("continue_url") or request.query_params.get(
            "continue_url"
        )
        cancel_url = request.data.get("cancel_url") or request.query_params.get(
            "cancel_url", "https://www.fightpaper.com/?q=ohno"
        )

        try:
            if domain_id and professional_user_id:
                user_domain = UserDomain.objects.get(id=domain_id)
                professional_user = ProfessionalUser.objects.get(
                    id=professional_user_id
                )
                email = professional_user.user.email
            elif (domain_name or domain_phone) and user_email:
                domain_id = resolve_domain_id(
                    domain_name=domain_name, phone_number=domain_phone
                )
                user_domain = UserDomain.objects.get(id=domain_id)
                professional_user = User.objects.get(email=user_email).professionaluser
                email = user_email
            else:
                return Response(
                    common_serializers.ErrorSerializer(
                        {"error": "Missing required parameters"}
                    ).data,
                    status=status.HTTP_400_BAD_REQUEST,
                )

            checkout_session = self.create_stripe_checkout_session(
                email, professional_user.id, user_domain, continue_url, cancel_url
            )
            return Response(
                serializers.FinishPaymentResponseSerializer(
                    {"next_url": checkout_session.url}
                ).data,
                status=status.HTTP_200_OK,
            )
        except UserDomain.DoesNotExist:
            return Response(
                common_serializers.ErrorSerializer({"error": "Domain not found"}).data,
                status=status.HTTP_400_BAD_REQUEST,
            )
        except ProfessionalUser.DoesNotExist:
            return Response(
                common_serializers.ErrorSerializer(
                    {"error": "Professional user not found"}
                ).data,
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception as e:
            logger.opt(exception=e).error("Error in finishing payment")
            return Response(
                common_serializers.ErrorSerializer({"error": f"Error {e}"}).data,
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @transaction.atomic
    def perform_create(
        self, request: Request, serializer: Serializer
    ) -> Response:
        data: dict[str, bool | str | dict[str, str]] = serializer.validated_data  # type: ignore
        user_signup_info: dict[str, str] = data["user_signup_info"]  # type: ignore
        domain_name: Optional[str] = user_signup_info["domain_name"]  # type: ignore
        visible_phone_number: Optional[str] = user_signup_info["visible_phone_number"]  # type: ignore
        new_domain: bool = bool(data["make_new_domain"])  # type: ignore
        user_domain_opt: Optional[UserDomain] = None

        if not validate_password(user_signup_info["password"]):
            return Response(
                common_serializers.ErrorSerializer({"error": "Invalid password"}).data,
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not new_domain:
            # In practice the serializer may enforce these for us
            try:
                if not domain_name or len(domain_name) == 0:
                    raise UserDomain.DoesNotExist()
                user_domain_opt = UserDomain.find_by_name(name=domain_name).get()
            except UserDomain.DoesNotExist:
                try:
                    user_domain_opt = UserDomain.objects.get(
                        visible_phone_number=visible_phone_number
                    )
                except UserDomain.DoesNotExist:
                    logger.opt(exception=True).error(
                        f"Error finding domain {domain_name} / {visible_phone_number}"
                    )
                    return Response(
                        common_serializers.ErrorSerializer(
                            {
                                "error": f"Can not join missing domain {domain_name} / {visible_phone_number} it does not exist"
                            }
                        ).data,
                        status=status.HTTP_400_BAD_REQUEST,
                    )
        else:
            if UserDomain.find_by_name(name=domain_name).count() != 0:
                return Response(
                    common_serializers.ErrorSerializer(
                        {"error": "Domain already exists"}
                    ).data,
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if (
                UserDomain.objects.filter(
                    visible_phone_number=visible_phone_number
                ).count()
                != 0
            ):
                return Response(
                    common_serializers.ErrorSerializer(
                        {
                            "error": "Visible phone number already exists",
                        }
                    ).data,
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if "user_domain" not in data:
                return Response(
                    common_serializers.ErrorSerializer(
                        {
                            "error": "Need domain info when making a new domain or solo provider",
                        }
                    ).data,
                    status=status.HTTP_400_BAD_REQUEST,
                )
            user_domain_info: dict[str, Optional[str]] = data["user_domain"]  # type: ignore
            if domain_name != user_domain_info["name"]:
                if user_domain_info["name"] is None:
                    user_domain_info["name"] = domain_name
                else:
                    return Response(
                        common_serializers.ErrorSerializer(
                            {
                                "error": "Domain name and user domain name must match",
                            }
                        ).data,
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            # We want to allow null
            if user_domain_info["name"] == "":
                user_domain_info["name"] = None
            if visible_phone_number != user_domain_info["visible_phone_number"]:
                if user_domain_info["visible_phone_number"] is None:
                    user_domain_info["visible_phone_number"] = visible_phone_number
                else:
                    return Response(
                        common_serializers.ErrorSerializer(
                            {
                                "error": "Visible phone number and user domain visible phone number must match",
                            }
                        ).data,
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            user_domain_opt = UserDomain.objects.create(
                active=False,
                **user_domain_info,
            )

        if not user_domain_opt:
            raise Exception("No user domain found or created")
        user_domain: UserDomain = user_domain_opt  # type: ignore
        raw_username: str = user_signup_info["email"]  # type: ignore
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

        if not (settings.DEBUG and data["skip_stripe"]):
            checkout_session = self.create_stripe_checkout_session(
                email,
                professional_user.id,
                user_domain,
                user_signup_info["continue_url"],
                user_signup_info.get(
                    "cancel_url", "https://www.fightpaperwork.com/?q=ohno"
                ),
                card_required=data["card_required"],
            )
            extra_user_properties = ExtraUserProperties.objects.create(
                user=user, email_verified=False
            )
            subscription_id = checkout_session.subscription
            return Response(serializers.ProfessionalSignupResponseSerializer(
                {"next_url": checkout_session.url}
            ).data, status=status.HTTP_201_CREATED)
        else:
            return Response(serializers.ProfessionalSignupResponseSerializer(
                {"next_url": "https://www.fightpaperwork.com/?q=testmode"}
            ).data, status=status.HTTP_201_CREATED)


class RestLoginView(ViewSet, SerializerMixin):
    serializer_class = serializers.LoginFormSerializer

    @extend_schema(responses=serializers.StatusResponseSerializer)
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
            # First check the login
            user_domain = UserDomain.objects.get(id=domain_id)
            username = combine_domain_and_username(
                raw_username, phone_number=phone, domain_id=domain_id
            )
            user = authenticate(username=username, password=password)
            # If we have a valid user check if the domain is active.
            if (
                user
                and not user_domain.active
                and not user_domain.stripe_subscription_id
                and not user_domain.stripe_customer_id
            ):
                return Response(
                    common_serializers.NotPaidErrorSerializer().data,
                    status=status.HTTP_401_UNAUTHORIZED,
                )
        except Exception as e:
            return Response(
                common_serializers.ErrorSerializer(
                    {
                        "error": f"Domain or phone number not found -- {e}",
                    }
                ).data,
                status=status.HTTP_400_BAD_REQUEST,
            )
        user = authenticate(username=username, password=password)
        if user:
            request.session["domain_id"] = domain_id
            logger.info(
                f"User {user.username} logged in setting domain id to {domain_id}"
            )
            login(request, user)
            return Response(
                serializers.StatusResponseSerializer({"status": "success"}).data
            )
        try:
            user = User.objects.get(username=username)
            if not user.is_active:
                return Response(
                    common_serializers.ErrorSerializer(
                        {
                            "error": "User is inactive -- please verify your e-mail",
                        }
                    ).data,
                    status=status.HTTP_401_UNAUTHORIZED,
                )
        except User.DoesNotExist:
            pass
        return Response(
            common_serializers.ErrorSerializer({"error": f"Invalid credentials"}).data,
            status=status.HTTP_401_UNAUTHORIZED,
        )


class PatientUserViewSet(ViewSet, CreateMixin):
    """Create a new patient user."""

    def get_serializer_class(self):
        if self.action == "create":
            return serializers.CreatePatientUserSerializer
        else:
            return serializers.GetOrCreatePendingPatientSerializer

    @extend_schema(
        responses={
            200: serializers.StatusResponseSerializer,
            201: serializers.StatusResponseSerializer,
            400: common_serializers.ErrorSerializer,
            403: common_serializers.ErrorSerializer,
        }
    )
    def create(self, request: Request) -> Response:
        try:
            return super().create(request)
        except UserDomain.DoesNotExist as e:
            # Handle domain not found errors with a user-friendly message
            logger.opt(exception=True).error(
                f"Domain not found error when creating patient user: {str(e)}"
            )
            return Response(
                common_serializers.ErrorSerializer(
                    {
                        "error": "The specified healthcare provider was not found, ask them to sign up for Fight Paperwork."
                    }
                ).data,
                status=status.HTTP_400_BAD_REQUEST,
            )
        except django_db_utils.IntegrityError as e:
            # Check for uniqueness constraint errors (typically email or username conflicts)
            if (
                "unique constraint" in str(e).lower()
                or "duplicate key" in str(e).lower()
            ):
                logger.opt(exception=True).error(
                    f"Uniqueness constraint error when creating patient user: {str(e)}"
                )
                return Response(
                    common_serializers.ErrorSerializer(
                        {"error": "A user with this email already exists"}
                    ).data,
                    status=status.HTTP_400_BAD_REQUEST,
                )
            # Log but pass through other integrity errors
            logger.opt(exception=True).error(
                f"Database integrity error when creating patient user: {str(e)}"
            )
            return Response(
                common_serializers.ErrorSerializer(
                    {"error": f"Database error: {str(e)}"}
                ).data,
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception as e:
            # Catch all other exceptions to provide friendlier responses
            logger.opt(exception=True).error(
                f"Unexpected error when creating patient user: {str(e)}"
            )
            return Response(
                common_serializers.ErrorSerializer({"error": str(e)}).data,
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @extend_schema(responses=serializers.StatusResponseSerializer)
    def perform_create(self, request: Request, serializer) -> Response:
        serializer.is_valid(raise_exception=True)
        domain_name: Optional[str] = None
        provider_phone_number: Optional[str] = None
        patient_phone_number: Optional[str] = None
        validated_data = serializer.validated_data
        if "domain_name" in validated_data:
            domain_name = validated_data.pop("domain_name")
        if "provider_phone_number" in validated_data:
            provider_phone_number = validated_data.pop("provider_phone_number")
        if "patient_phone_number" in validated_data:
            patient_phone_number = validated_data.pop("patient_phone_number")
        domain_id = auth_utils.get_domain_id_from_request(request)
        user = create_user(
            email=validated_data["email"],
            raw_username=validated_data["username"],
            first_name=validated_data.get("firstname", ""),
            last_name=validated_data.get("lastname", ""),
            domain_name=domain_name,
            phone_number=provider_phone_number,
            password=validated_data["password"],
            domain_id=domain_id,
        )
        country = "USA"
        if "country" in validated_data:
            country = validated_data["country"]
        state = None
        if "state" in validated_data:
            state = validated_data["state"]
        city = None
        if "city" in validated_data:
            city = validated_data["city"]
        address1 = None
        if "address1" in validated_data:
            address1 = validated_data["address1"]
        address2 = None
        if "address2" in validated_data:
            address2 = validated_data["address2"]
        zipcode = None
        if "zipcode" in validated_data:
            zipcode = validated_data["zipcode"]

        UserContactInfo.objects.create(
            user=user,
            phone_number=patient_phone_number,
            country=country,
            state=state,
            city=city,
            address1=address1,
            address2=address2,
            zipcode=zipcode,
        )

        PatientUser.objects.create(user=user, active=False)

        extra_user_properties = ExtraUserProperties.objects.create(
            user=user, email_verified=False
        )
        send_verification_email(request, user)
        return Response(
            serializers.StatusResponseSerializer({"status": "pending"}).data
        )

    @extend_schema(responses=serializers.PatientReferenceSerializer)
    @action(detail=False, methods=["post", "options"])
    def get_or_create_pending(self, request: Request) -> Response:
        serializer = self.deserialize(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = None
        # We allow for creation without a patient e-mail but we make a fake ID
        if "username" in serializer.validated_data:
            email = serializer.validated_data["username"]
        if not email or len(email) == 0:
            email = get_next_fake_username()
        domain = UserDomain.objects.get(
            id=auth_utils.get_domain_id_from_request(request)
        )
        user = get_patient_or_create_pending_patient(
            email=email,
            raw_username=email,
            domain=domain,
            fname=serializer.validated_data["first_name"],
            lname=serializer.validated_data["last_name"],
        )
        response_serializer = serializers.PatientReferenceSerializer(
            {"id": user.id, "email": email}
        )
        return Response(response_serializer.data)


class VerifyEmailViewSet(ViewSet, SerializerMixin):
    """
    Handles email verification and resending verification emails.
    """

    serializer_class = serializers.VerificationTokenSerializer

    @extend_schema(responses=serializers.StatusResponseSerializer)
    @action(detail=False, methods=["post"])
    def verify(self, request: Request) -> Response:
        serializer = self.deserialize(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            uid = serializer.validated_data["user_id"]
            user = User.objects.get(pk=uid)
        except (TypeError, ValueError, OverflowError, User.DoesNotExist) as e:
            return Response(
                common_serializers.ErrorSerializer(
                    {
                        "error": "Invalid activation link [user not found]",
                    }
                ).data,
                status=status.HTTP_400_BAD_REQUEST,
            )
        token = serializer.validated_data["token"]
        try:
            verification_token = VerificationToken.objects.get(user=user, token=token)
            if timezone.now() > verification_token.expires_at:
                return Response(
                    common_serializers.ErrorSerializer(
                        {"error": "Activation link has expired"}
                    ).data,
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
            return Response(
                serializers.StatusResponseSerializer({"status": "success"}).data
            )
        except VerificationToken.DoesNotExist as e:
            return Response(
                common_serializers.ErrorSerializer(
                    {"error": "Invalid activation link"}
                ).data,
                status=status.HTTP_400_BAD_REQUEST,
            )

    @extend_schema(responses=serializers.StatusResponseSerializer)
    @action(detail=False, methods=["post"])
    def resend(self, request: Request) -> Response:
        """
        Resends verification email for unverified users.
        """
        serializer = self.deserialize(data=request.data)
        serializer.is_valid(raise_exception=True)
        user_id = serializer.validated_data["user_id"]
        user = User.objects.get(pk=user_id)
        send_verification_email(request, user)
        return Response(
            serializers.StatusResponseSerializer(
                {"status": "verification email resent"}
            ).data
        )


class PasswordResetViewSet(ViewSet, SerializerMixin):
    """
    Handles password reset requests and completion.
    """

    def get_serializer_class(self):
        if self.action == "finish_reset":
            return serializers.FinishPasswordResetFormSerializer
        return serializers.RequestPasswordResetFormSerializer

    @extend_schema(
        responses={
            200: serializers.StatusResponseSerializer,
            400: common_serializers.ErrorSerializer,
        }
    )
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

            return Response(
                serializers.StatusResponseSerializer({"status": "reset_requested"}).data
            )

        except User.DoesNotExist:
            logger.debug(f"User does not exist")
            return Response(
                common_serializers.ErrorSerializer(
                    {"error": "User does not exist"}
                ).data,
                status=status.HTTP_400_BAD_REQUEST,
            )

        except UserDomain.DoesNotExist:
            logger.debug(f"User domain does not exist")
            return Response(
                common_serializers.ErrorSerializer(
                    {
                        "error": "User domain does not exist -- check provider phone number"
                    }
                ).data,
                status=status.HTTP_400_BAD_REQUEST,
            )

        except Exception as e:
            logger.opt(exception=e).error(
                f"Password reset request failed with unexpected error"
            )
            return Response(
                common_serializers.ErrorSerializer({"error": str(e)}).data,
                status=status.HTTP_400_BAD_REQUEST,
            )

    @extend_schema(
        responses={
            200: serializers.StatusResponseSerializer,
            400: common_serializers.ErrorSerializer,
        }
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
                    common_serializers.ErrorSerializer(
                        {"error": "Reset token has expired"}
                    ).data,
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Update password
            user = reset_token.user
            if not validate_password(data["new_password"]):
                return Response(
                    common_serializers.ErrorSerializer(
                        {"error": "Invalid password"}
                    ).data,
                    status=status.HTTP_400_BAD_REQUEST,
                )
            user.set_password(data["new_password"])
            user.save()

            # Clean up token
            reset_token.delete()

            return Response(
                serializers.StatusResponseSerializer(
                    {"status": "password_reset_complete"}
                ).data
            )

        except ResetToken.DoesNotExist:
            return Response(
                common_serializers.ErrorSerializer(
                    {"error": "Invalid reset token"}
                ).data,
                status=status.HTTP_400_BAD_REQUEST,
            )
