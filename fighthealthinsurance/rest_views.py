import asyncio
import json
import typing
from typing import Optional

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import SuspiciousFileOperation
from django.db import IntegrityError, models
from django.db.models import Count, Q
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone

from asgiref.sync import async_to_sync, sync_to_async
from dateutil.relativedelta import relativedelta
from django_encrypted_filefield.crypt import Cryptographer
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from loguru import logger
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from stopit import ThreadingTimeout as Timeout

from fhi_users.auth import auth_utils
from fhi_users.models import PatientUser, ProfessionalUser, UserDomain
from fighthealthinsurance import common_view_logic, rest_serializers as serializers
from fighthealthinsurance.ml.health_status import health_status
from fighthealthinsurance.ml.ml_router import ml_router
from fighthealthinsurance.models import (
    Appeal,
    AppealAttachment,
    ChooserCandidate,
    ChooserSkip,
    ChooserTask,
    ChooserVote,
    DemoRequests,
    Denial,
    DenialQA,
    MailingListSubscriber,
    OngoingChat,
    PriorAuthRequest,
    ProposedPriorAuth,
    PubMedMiniArticle,
    SecondaryAppealProfessionalRelation,
)
from fighthealthinsurance.pubmed_tools import PubMedTools
from fighthealthinsurance.rest_mixins import (
    CreateMixin,
    DeleteMixin,
    DeleteOnlyMixin,
    SerializerMixin,
)

from .common_view_logic import AppealAssemblyHelper
from .utils import is_convertible_to_int

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()

appeal_assembly_helper = AppealAssemblyHelper()
pubmed_tools = PubMedTools()


class ChatViewSet(viewsets.ViewSet):
    """
    ViewSet for managing ongoing chats with the LLM assistant.

    Lists all chats for the authenticated professional user, ordered by most recently updated.
    Provides metadata about each chat including the first user message preview.
    """

    def list(self, request):
        """List all chats for the current professional user."""
        user: User = request.user  # type: ignore

        try:
            professional_user = ProfessionalUser.objects.get(user=user, active=True)
        except ProfessionalUser.DoesNotExist:
            logger.warning(f"Professional user not found for user {user.id}")
            return Response({"error": "Professional user not found"}, status=404)

        # Get all chats for this professional user, ordered by most recently updated
        chats = OngoingChat.objects.filter(
            professional_user=professional_user
        ).order_by("-updated_at")

        # Prepare the response data
        chat_list = []
        for chat in chats:
            # Extract the first user message (if it exists)
            first_message_preview = ""
            if chat.chat_history and len(chat.chat_history) > 0:
                for message in chat.chat_history:
                    if message.get("role") == "user":
                        content = message.get("content", "")
                        first_message_preview = content[:100] + (
                            "..." if len(content) > 100 else ""
                        )
                        break

            # Get timestamps
            created_at = chat.created_at if hasattr(chat, "created_at") else None
            updated_at = chat.updated_at if hasattr(chat, "updated_at") else None

            # Get the date from the most recent message if available
            last_message_date = None
            if chat.chat_history and len(chat.chat_history) > 0:
                for message in reversed(chat.chat_history):
                    if "timestamp" in message:
                        try:
                            last_message_date = message["timestamp"]
                            break
                        except (ValueError, TypeError):
                            pass

            chat_list.append(
                {
                    "id": str(chat.id),
                    "created_at": created_at,
                    "updated_at": updated_at or last_message_date,
                    "message_preview": first_message_preview,
                    "message_count": len(chat.chat_history) if chat.chat_history else 0,
                    "title": self._generate_chat_title(chat),
                }
            )

        return Response(chat_list)

    @action(detail=True, methods=["delete"])
    def delete(self, request, pk=None):
        """Delete a specific chat."""
        user: User = request.user  # type: ignore

        try:
            professional_user = ProfessionalUser.objects.get(user=user, active=True)
        except ProfessionalUser.DoesNotExist:
            logger.warning(f"Professional user not found for user {user.id}")
            return Response({"error": "Professional user not found"}, status=404)

        try:
            chat = OngoingChat.objects.get(id=pk, professional_user=professional_user)
            chat.delete()
            return Response(
                {"status": "success", "message": "Chat deleted successfully"}
            )
        except OngoingChat.DoesNotExist:
            return Response({"error": "Chat not found"}, status=404)

    def _generate_chat_title(self, chat):
        """Generate a title for the chat based on its content."""
        if not chat.chat_history or len(chat.chat_history) == 0:
            return "New conversation"

        # Try to find the first user message
        for message in chat.chat_history:
            if message.get("role") == "user":
                content = message.get("content", "")
                # Extract first line or first few words
                title = ""
                if "\n" in content:
                    title = content.split("\n")[0][:50]
                else:
                    words = content.split()
                    title = " ".join(words[: min(5, len(words))])

                if len(title) > 50:
                    title = title[:47] + "..."

                # Ensure the title is not empty
                if len(title) > 5:
                    return title

        return "Life, the universe and everything? 42"


class DataRemovalViewSet(viewsets.ViewSet, DeleteMixin, DeleteOnlyMixin):
    """
    ViewSet for handling data removal requests.
    Allows users to request deletion of all their data by email address.
    """

    serializer_class = serializers.DeleteDataFormSerializer

    @extend_schema(
        responses={204: serializers.SuccessSerializer, 400: serializers.ErrorSerializer}
    )
    def perform_delete(self, request: Request, serializer):
        email: str = serializer.validated_data["email"]
        common_view_logic.RemoveDataHelper.remove_data_for_email(email)
        return Response(
            serializers.SuccessSerializer(
                {"message": "Data deleted successfully"}
            ).data,
            status=status.HTTP_204_NO_CONTENT,
        )


class HealthHistoryViewSet(viewsets.ViewSet, CreateMixin):
    """
    ViewSet for updating patient health history on a denial.

    Accepts health context information and updates the associated denial record.
    """

    serializer_class = serializers.HealthHistoryFormSerializer

    @extend_schema(responses=serializers.StatusResponseSerializer)
    def create(self, request: Request) -> Response:
        return super().create(request)

    @extend_schema(responses=serializers.StatusResponseSerializer)
    def perform_create(self, request: Request, serializer) -> Response:
        logger.debug(f"Updating denial with {serializer.validated_data}")
        common_view_logic.DenialCreatorHelper.update_denial(
            **serializer.validated_data,
        )

        return Response(
            serializers.SuccessSerializer({"message": "Updated health history"}).data,
            status=status.HTTP_201_CREATED,
        )


class NextStepsViewSet(viewsets.ViewSet, CreateMixin):
    """
    ViewSet for determining next steps after denial information is collected.

    Analyzes the denial data and returns recommended actions, appeal options,
    and relevant regulatory information.
    """

    serializer_class = serializers.PostInferedFormSerializer

    @extend_schema(
        responses={
            200: serializers.NextStepInfoSerizableSerializer,
            201: serializers.NextStepInfoSerizableSerializer,
            400: serializers.ErrorSerializer,
        }
    )
    def create(self, request: Request) -> Response:
        return super().create(request)

    def perform_create(self, request: Request, serializer) -> Response:
        next_step_info = common_view_logic.FindNextStepsHelper.find_next_steps(
            **serializer.validated_data
        )

        return Response(
            serializers.NextStepInfoSerizableSerializer(
                next_step_info.convert_to_serializable(),
            ).data,
            status=status.HTTP_201_CREATED,
        )


class DenialViewSet(viewsets.ViewSet, CreateMixin):
    """
    ViewSet for creating and managing insurance denial records.

    Supports creating new denials, retrieving existing ones, and managing
    associated PubMed articles for evidence. Handles professional-created
    denials with patient associations and mailing list subscriptions.
    """

    serializer_class = serializers.DenialFormSerializer

    def get_serializer_class(self):
        if self.action == "create":
            return serializers.DenialFormSerializer
        elif self.action == "get_candidate_articles":
            return serializers.GetCandidateArticlesSerializer
        elif self.action == "select_articles":
            return serializers.SelectContextArticlesSerializer
        else:
            return None

    @extend_schema(responses=serializers.DenialResponseInfoSerializer)
    def create(self, request: Request) -> Response:
        logger.debug("Routing create through parent...")
        return super().create(request)

    @extend_schema(responses=serializers.DenialResponseInfoSerializer)
    def retrieve(self, request: Request, pk: int) -> Response:
        current_user: User = request.user  # type: ignore
        denial = get_object_or_404(
            Denial.filter_to_allowed_denials(current_user), pk=pk
        )
        denial_response_info = (
            common_view_logic.DenialCreatorHelper.format_denial_response_info(denial)
        )
        response_serializer = serializers.DenialResponseInfoSerializer(
            instance=denial_response_info
        )
        return Response(response_serializer.data)

    @extend_schema(responses=serializers.DenialResponseInfoSerializer)
    def perform_create(self, request: Request, serializer) -> Response:
        current_user: User = request.user  # type: ignore
        creating_professional = ProfessionalUser.objects.get(user=current_user)
        serializer = self.deserialize(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer_data = serializer.validated_data
        logger.debug(f"Using data {serializer_data}")
        if (
            "primary_professional" in serializer_data
            and serializer_data["primary_professional"] is not None
            and is_convertible_to_int(serializer_data["primary_professional"])
        ):
            primary_professional = ProfessionalUser.objects.get(
                id=serializer_data.pop("primary_professional")
            )
            serializer_data["primary_professional"] = primary_professional
        denial: Optional[Denial] = None
        if "denial_id" in serializer_data:
            denial_id = serializer_data.pop("denial_id")
            if denial_id and is_convertible_to_int(denial_id):
                logger.debug(f"Looking up existing denial {denial_id}")
                denial_id = int(denial_id)
                denial = Denial.filter_to_allowed_denials(current_user).get(
                    denial_id=denial_id
                )
            elif denial_id and denial_id != "":
                logger.debug(f"Unexpected format of denial id {denial_id}")
            else:
                # Denial ID provided but is None
                pass
        else:
            logger.debug("No denial id present, will make new one.")
        if "patient_id" in serializer_data and is_convertible_to_int(
            serializer_data["patient_id"]
        ):
            patient_id = serializer_data.pop("patient_id")
            if patient_id:
                serializer_data["patient_user"] = PatientUser.objects.get(id=patient_id)
            if (
                "email" not in serializer_data
                or serializer_data["email"] is None
                or len(serializer_data["email"]) == 0
            ):
                serializer_data["email"] = serializer_data["patient_user"].user.email
        # Handle subscription separately - pop from serializer data before passing
        subscribe = serializer_data.pop("subscribe", False)

        # Extract tracking info for analytics (privacy-aware)
        from fhi_users.audit import extract_tracking_info

        tracking_info = extract_tracking_info(
            request=request, is_professional=(creating_professional is not None)
        )

        denial_response_info = (
            common_view_logic.DenialCreatorHelper.create_or_update_denial(
                denial=denial,
                creating_professional=creating_professional,
                tracking_info=tracking_info,
                **serializer_data,
            )
        )
        # Handle mailing list subscription if requested
        if subscribe and "email" in serializer_data:
            try:
                email = serializer_data["email"]
                if not MailingListSubscriber.objects.filter(email=email).exists():
                    MailingListSubscriber.objects.create(
                        email=email,
                        comments="Subscribed via denial form",
                    )
            except Exception as e:
                logger.warning(f"Failed to subscribe email to mailing list: {e}")
        denial = Denial.objects.get(uuid=denial_response_info.uuid)
        # Creating a pending appeal if one doesn't exist
        try:
            Appeal.objects.get(for_denial=denial)
        except Appeal.DoesNotExist:
            appeal = Appeal.objects.create(
                for_denial=denial,
                patient_user=denial.patient_user,
                primary_professional=denial.primary_professional,
                creating_professional=denial.creating_professional,
                pending=True,
            )
            denial_response_info.appeal_id = appeal.id
        return Response(
            serializers.DenialResponseInfoSerializer(
                instance=denial_response_info
            ).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(responses=serializers.PubMedMiniArticleSerializer(many=True))
    @action(detail=False, methods=["post"])
    def get_candidate_articles(self, request: Request) -> Response:
        """Get candidate PubMed articles for a denial based on diagnosis and procedure."""
        serializer = self.deserialize(data=request.data)
        serializer.is_valid(raise_exception=True)

        current_user: User = request.user  # type: ignore
        denial = get_object_or_404(
            Denial.filter_to_allowed_denials(current_user),
            denial_id=serializer.validated_data["denial_id"],
        )

        articles = async_to_sync(pubmed_tools.find_pubmed_articles_for_denial)(denial)

        return Response(
            serializers.PubMedMiniArticleSerializer(articles, many=True).data,
            status=status.HTTP_200_OK,
        )

    @extend_schema(responses=serializers.StatusResponseSerializer)
    @action(detail=False, methods=["post"])
    def select_articles(self, request: Request) -> Response:
        """Select PubMed articles to include in the denial context."""
        serializer = self.deserialize(data=request.data)
        serializer.is_valid(raise_exception=True)

        current_user: User = request.user  # type: ignore
        denial = get_object_or_404(
            Denial.filter_to_allowed_denials(current_user),
            denial_id=serializer.validated_data["denial_id"],
        )

        pmids = serializer.validated_data["pmids"]
        denial.pubmed_ids_json = pmids
        denial.save()
        return Response(
            serializers.SuccessSerializer(
                {"message": f"Selected {len(pmids)} articles for this context"}
            ).data,
            status=status.HTTP_200_OK,
        )


class QAResponseViewSet(viewsets.ViewSet, CreateMixin):
    """
    ViewSet for storing question-and-answer responses related to a denial.

    Saves user-provided answers to appeal-related questions, which are used
    to provide context for appeal generation.
    """

    serializer_class = serializers.QAResponsesSerializer

    @extend_schema(responses=serializers.StatusResponseSerializer)
    def create(self, request: Request) -> Response:
        return super().create(request)

    @extend_schema(responses=serializers.StatusResponseSerializer)
    def perform_create(self, request: Request, serializer) -> Response:
        user: User = request.user  # type: ignore
        denial = Denial.filter_to_allowed_denials(user).get(
            denial_id=serializer.validated_data["denial_id"]
        )
        for key, value in serializer.validated_data["qa"].items():
            if not key or not value or len(value) == 0:
                continue
            try:
                dqa = DenialQA.objects.filter(denial=denial).get(question=key)
                dqa.text_answer = value
                dqa.save()
            except DenialQA.DoesNotExist:
                DenialQA.objects.create(
                    denial=denial,
                    question=key,
                    text_answer=value,
                )
        qa_context = {}
        for key, value in DenialQA.objects.filter(denial=denial).values_list(
            "question", "text_answer"
        ):
            qa_context[key] = value
        denial.qa_context = json.dumps(qa_context)
        denial.save()
        return Response(status=status.HTTP_204_NO_CONTENT)


class FollowUpViewSet(viewsets.ViewSet, CreateMixin):
    """
    ViewSet for recording follow-up outcomes on appeals.

    Stores user-reported results of their appeal (success, denial, etc.)
    to track appeal effectiveness and outcomes.
    """

    serializer_class = serializers.FollowUpFormSerializer

    @extend_schema(responses=serializers.StatusResponseSerializer)
    def create(self, request: Request) -> Response:
        return super().create(request)

    @extend_schema(responses=serializers.StatusResponseSerializer)
    def perform_create(self, request: Request, serializer) -> Response:
        common_view_logic.FollowUpHelper.store_follow_up_result(
            **serializer.validated_data
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class Ping(APIView):
    """Simple health check endpoint that returns 204 No Content."""

    @extend_schema(responses=serializers.StatusResponseSerializer)
    def get(self, request: Request) -> Response:
        return Response(status=status.HTTP_204_NO_CONTENT)


class CheckStorage(APIView):
    """Health check endpoint that verifies external storage is accessible."""

    @extend_schema(responses=serializers.StatusResponseSerializer)
    def get(self, request: Request) -> Response:
        es = settings.EXTERNAL_STORAGE
        with Timeout(2.0):
            es.listdir("./")
            return Response(status=status.HTTP_204_NO_CONTENT)
        return Response(status=status.HTTP_503_SERVICE_UNAVAILABLE)


class CheckMlBackend(APIView):
    """Health check endpoint that verifies ML backend models are operational."""

    @extend_schema(responses=serializers.StatusResponseSerializer)
    def get(self, request: Request) -> Response:
        if ml_router.working():
            return Response(status=status.HTTP_204_NO_CONTENT)

        return Response(status=status.HTTP_503_SERVICE_UNAVAILABLE)


class LiveModelsStatus(APIView):
    """Return cached count of alive models/backends with last check timestamp.

    Shape:
    { "alive_models": int, "last_checked": epoch_seconds, "details": [{"name": str, "ok": bool, "error": Optional[str]}] }
    """

    @extend_schema(responses=serializers.LiveModelsStatusSerializer)
    def get(self, request: Request) -> Response:
        try:
            snapshot = health_status.get_snapshot()
            # Always succeed; return 200 with the current snapshot (even if 0)
            response = Response(snapshot, status=status.HTTP_200_OK)
            # Add cache control headers - cache for 5 minutes
            response["Cache-Control"] = "public, max-age=300"
            return response
        except Exception as e:
            logger.warning(f"LiveModelsStatus failed: {e}")
            # Graceful degradation: return zeros
            return Response(
                {
                    "alive_models": 0,
                    "last_checked": None,
                    "details": [],
                    "message": "status temporarily unavailable",
                },
                status=status.HTTP_200_OK,
            )


class AppealViewSet(viewsets.ViewSet, SerializerMixin):
    """
    ViewSet for managing appeals and related operations.

    Provides endpoints for listing, retrieving, assembling, and sending appeals.
    Supports fax transmission, patient notifications, provider invitations,
    PubMed article selection, search, and appeal statistics (both relative and absolute).
    """

    appeal_assembly_helper = AppealAssemblyHelper()

    def get_serializer_class(self):
        if self.action == "list":
            return serializers.AppealListRequestSerializer
        elif self.action == "send_fax":
            return serializers.SendFax
        elif self.action == "assemble_appeal":
            return serializers.AssembleAppealRequestSerializer
        elif self.action == "notify_patient":
            return serializers.NotifyPatientRequestSerializer
        elif self.action == "invite_provider":
            return serializers.InviteProviderSerializer
        elif self.action == "select_articles":
            return serializers.SelectAppealArticlesSerializer
        else:
            return None

    @extend_schema(responses=serializers.StatusResponseSerializer)
    @action(detail=False, methods=["post"])
    def select_articles(self, request: Request) -> Response:
        """Select PubMed articles to include in the fax."""
        serializer = self.deserialize(data=request.data)
        serializer.is_valid(raise_exception=True)

        current_user: User = request.user  # type: ignore
        appeal = get_object_or_404(
            Appeal.filter_to_allowed_appeals(current_user),
            id=serializer.validated_data["appeal_id"],
        )

        pmids = serializer.validated_data["pmids"]
        appeal.pubmed_ids_json = pmids
        appeal.save()
        return Response(
            serializers.SuccessSerializer(
                {"message": f"Selected {len(pmids)} articles for this appeal"}
            ).data,
            status=status.HTTP_200_OK,
        )

    @extend_schema(responses=serializers.AppealSummarySerializer)
    def list(self, request: Request) -> Response:
        # Lets figure out what appeals they _should_ see
        current_user: User = request.user  # type: ignore
        appeals = Appeal.filter_to_allowed_appeals(current_user)
        # Optimize queries to avoid N+1: prefetch related objects used in serializer
        appeals = appeals.select_related(
            'for_denial',
            'primary_professional',
            'creating_professional',
            'patient_user',
            'chat',
        ).prefetch_related(
            'for_denial__denial_type',
        )
        # Parse the filters
        input_serializer = self.deserialize(data=request.data)
        # TODO: Handle the filters
        output_serializer = serializers.AppealSummarySerializer(appeals, many=True)
        return Response(output_serializer.data)

    @extend_schema(responses=serializers.AppealDetailSerializer)
    def retrieve(self, request: Request, pk: int) -> Response:
        current_user: User = request.user  # type: ignore
        appeal = get_object_or_404(
            Appeal.filter_to_allowed_appeals(current_user), pk=pk
        )
        serializer = serializers.AppealDetailSerializer(appeal)
        return Response(serializer.data)

    @extend_schema(
        responses={200: serializers.SuccessSerializer, 404: serializers.ErrorSerializer}
    )
    @action(detail=False, methods=["post"])
    def notify_patient(self, request: Request) -> Response:
        serializer = self.deserialize(request.data)
        if not serializer.is_valid():
            return Response(
                serializers.ErrorSerializer({"error": serializer.errors}).data,
                status=status.HTTP_400_BAD_REQUEST,
            )
        pk = serializer.validated_data["id"]
        include_professional = False
        if "include_professional" in serializer.validated_data:
            include_professional = serializer.validated_data["include_professional"]
        current_user: User = request.user  # type: ignore
        appeal = get_object_or_404(
            Appeal.filter_to_allowed_appeals(current_user), pk=pk
        )
        denial = appeal.for_denial
        if denial:
            denial.professional_to_finish = serializer.validated_data[
                "professional_to_finish"
            ]
        # Notifying the patient makes it visible.
        if not appeal.patient_visible:
            appeal.patient_visible = True
            appeal.save()
        patient_user: Optional[PatientUser] = appeal.patient_user
        if patient_user is None:
            return Response(
                serializers.ErrorSerializer({"error": "Patient not found"}).data,
                status=status.HTTP_404_NOT_FOUND,
            )
        professional_name = None
        if include_professional:
            professional_name = ProfessionalUser.objects.get(
                user=current_user
            ).get_display_name()
        user: User = patient_user.user
        if not user.is_active:
            # Send an invitation to sign up for an account (mention it's free)
            common_view_logic.PatientNotificationHelper.send_signup_invitation(
                email=user.email,
                professional_name=professional_name,
                practice_number=UserDomain.objects.get(
                    id=auth_utils.get_domain_id_from_request(request)
                ).visible_phone_number,
            )
        else:
            # Notify the patient that there's a free draft appeal to fill in
            common_view_logic.PatientNotificationHelper.notify_of_draft_appeal(
                email=user.email,
                professional_name=professional_name,
                practice_number=UserDomain.objects.get(
                    id=auth_utils.get_domain_id_from_request(request)
                ).visible_phone_number,
            )
        return Response(
            serializers.SuccessSerializer({"message": "Notification sent"}).data,
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        responses=serializers.AppealFullSerializer,
        parameters=[OpenApiParameter(name="pk", type=OpenApiTypes.INT)],
    )
    @action(detail=False, methods=["get"])
    def get_full_details(self, request: Request) -> Response:
        pk = request.query_params.get("pk")
        current_user: User = request.user  # type: ignore
        appeal = get_object_or_404(
            Appeal.filter_to_allowed_appeals(current_user), pk=pk
        )
        return Response(serializers.AppealFullSerializer(appeal).data)

    @extend_schema(responses=serializers.StatusResponseSerializer)
    @action(detail=False, methods=["post"])
    def send_fax(self, request: Request) -> Response:
        current_user: User = request.user  # type: ignore
        serializer = self.deserialize(data=request.data)
        serializer.is_valid(raise_exception=True)
        appeal_id = serializer.validated_data["appeal_id"]
        appeal = get_object_or_404(
            Appeal.filter_to_allowed_appeals(current_user), pk=appeal_id
        )
        if serializer["fax_number"] is not None:
            appeal.fax_number = serializer["fax_number"]
            appeal.save()
        patient_user = None
        try:
            patient_user = PatientUser.objects.get(user=current_user)
        except PatientUser.DoesNotExist:
            pass
        if (
            appeal.patient_user == patient_user
            and appeal.for_denial.professional_to_finish
        ):
            appeal.pending_patient = False
            appeal.pending_professional = True
            appeal.pending = True
            appeal.save()
            return Response(
                data=serializers.StatusResponseSerializer(
                    {
                        "message": "Pending professional",
                        "status": "pending_professional",
                    }
                ).data,
                status=status.HTTP_200_OK,
            )
        else:
            appeal.pending_patient = False
            appeal.pending_professional = False
            appeal.pending = False
            appeal.save()
            staged = common_view_logic.SendFaxHelper.stage_appeal_as_fax(
                appeal, email=current_user.email, professional=True
            )
            common_view_logic.SendFaxHelper.remote_send_fax(
                uuid=staged.uuid, hashed_email=staged.hashed_email
            )
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(responses=serializers.AssembleAppealResponseSerializer)
    @action(detail=False, methods=["post"])
    def assemble_appeal(self, request) -> Response:
        current_user: User = request.user  # type: ignore
        serializer = self.deserialize(data=request.data)
        serializer.is_valid(raise_exception=True)
        # Make sure the user has permission to this denial
        denial_uuid: Optional[str] = None
        denial_opt: Optional[Denial] = None
        if "denial_uuid" in serializer.validated_data:
            denial_uuid = serializer.validated_data["denial_uuid"]
        if denial_uuid:
            denial_opt = Denial.filter_to_allowed_denials(current_user).get(
                denial_uuid=denial_uuid
            )
        else:
            denial_id = serializer.validated_data["denial_id"]
            denial_opt = Denial.filter_to_allowed_denials(current_user).get(
                denial_id=denial_id
            )
        if denial_opt is None:
            raise Exception("Denial not found")
        denial: Denial = denial_opt  # type: ignore
        appeal = None
        try:
            appeal = Appeal.filter_to_allowed_appeals(current_user).get(
                for_denial=denial, pending=True
            )
        except Appeal.DoesNotExist:
            pass
        patient_user = denial.patient_user
        if patient_user is None:
            raise Exception("Patient user not found on denial")
        user_domain = UserDomain.objects.get(
            id=auth_utils.get_domain_id_from_request(request)
        )
        completed_appeal_text = serializer.validated_data["completed_appeal_text"]
        insurance_company = serializer.validated_data["insurance_company"] or ""
        fax_phone = ""
        if "fax_phone" in serializer.validated_data:
            fax_phone = serializer.validated_data["fax_phone"]
        if fax_phone is None:
            fax_phone = denial.fax_phone
        pubmed_articles_to_include = []
        if "pubmed_articles_to_include" in serializer.validated_data:
            pubmed_articles_to_include = serializer.validated_data[
                "pubmed_articles_to_include"
            ]
        include_cover = True
        if "include_cover" in serializer.validated_data:
            include_cover = serializer.validated_data["include_cover"]
        # Note: we can also set this on the denial when we connect the health history if either
        # is true its included.
        include_provided_health_history = False
        if "include_provided_health_history" in serializer.validated_data:
            include_provided_health_history = serializer.validated_data[
                "include_provided_health_history"
            ]
        patient_user = denial.patient_user
        patient_name: str = "unkown"
        if patient_user is not None:
            patient_name = patient_user.get_combined_name()
        logger.debug("Making the appeal go vroooom")
        appeal = self.appeal_assembly_helper.create_or_update_appeal(
            appeal=appeal,
            name=patient_name,
            insurance_company=insurance_company,
            fax_phone=fax_phone,
            completed_appeal_text=completed_appeal_text,
            pubmed_ids_parsed=pubmed_articles_to_include,
            company_name="Fight Paperwork",
            email=current_user.email,
            denial=denial,
            primary_professional=denial.primary_professional,
            creating_professional=denial.creating_professional,
            domain=user_domain,
            cover_template_path="faxes/fpw_cover.html",
            cover_template_string=user_domain.cover_template_string or None,
            company_phone_number="202-938-3266",
            company_fax_number="415-840-7591",
            patient_user=patient_user,
            include_provided_health_history=include_provided_health_history,
            include_cover=include_cover,  # for now -- make this a flag on appeal
        )
        appeal.save()
        return Response(
            serializers.AssembleAppealResponseSerializer({"appeal_id": appeal.id}).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(responses=serializers.StatusResponseSerializer)
    @action(detail=False, methods=["post"])
    def invite_provider(self, request: Request) -> Response:
        current_user: User = request.user  # type: ignore
        serializer = serializers.InviteProviderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        pk = serializer.validated_data["appeal_id"]
        appeal = get_object_or_404(
            Appeal.filter_to_allowed_appeals(current_user), pk=pk
        )
        professional_id = serializer.validated_data.get("professional_id")
        email = serializer.validated_data.get("email")

        if professional_id:
            professional = get_object_or_404(ProfessionalUser, id=professional_id)
            SecondaryAppealProfessionalRelation.objects.create(
                appeal=appeal, professional=professional
            )
        else:
            try:
                professional_user = ProfessionalUser.objects.get(user__email=email)
                SecondaryAppealProfessionalRelation.objects.create(
                    appeal=appeal, professional=professional_user
                )
            except ProfessionalUser.DoesNotExist:
                inviting_professional = ProfessionalUser.objects.get(user=current_user)
                common_view_logic.ProfessionalNotificationHelper.send_signup_invitation(
                    email=email,
                    professional_name=inviting_professional.get_display_name(),
                    practice_number=UserDomain.objects.get(
                        id=auth_utils.get_domain_id_from_request(request)
                    ).visible_phone_number,
                )

        return Response(
            serializers.SuccessSerializer(
                {"message": "Provider invited successfully"}
            ).data,
            status=status.HTTP_200_OK,
        )

    @extend_schema(responses=serializers.StatisticsSerializer)
    @action(detail=False, methods=["get"])
    def stats(self, request: Request) -> Response:
        """Get relative statistics with comparison to previous period"""
        now = timezone.now()
        user: User = request.user  # type: ignore
        delta = request.GET.get("delta", "MoM")  # Default to Month over Month

        current_period_start = now
        current_period_end = now

        # Define previous period based on delta parameter
        if delta == "YoY":  # Year over Year
            current_period_start = current_period_start - relativedelta(years=1)
            previous_period_start = current_period_start - relativedelta(years=1)
        elif delta == "QoQ":  # Quarter over Quarter
            current_period_start = current_period_start - relativedelta(months=3)
            previous_period_start = current_period_start - relativedelta(months=3)
        else:  # MoM
            # Default to Month over Month if invalid delta
            current_period_start = current_period_start - relativedelta(months=1)
            previous_period_start = current_period_start - relativedelta(months=1)
        # Regardless end the previous period one microsend before the start of the current
        previous_period_end = current_period_start - relativedelta(microseconds=1)

        # Get user domain to calculate patients
        domain_id = auth_utils.get_domain_id_from_request(request)
        user_domain = None
        if domain_id:
            try:
                user_domain = UserDomain.objects.get(id=domain_id)
            except UserDomain.DoesNotExist:
                pass

        # Get current period statistics
        current_appeals = Appeal.filter_to_allowed_appeals(user).filter(
            creation_date__range=(
                current_period_start.date(),
                current_period_end.date(),
            )
        )
        current_total = current_appeals.count()
        current_pending = current_appeals.filter(pending=True).count()
        current_sent = current_appeals.filter(sent=True).count()

        # Use success field instead of response_date
        current_successful = current_appeals.filter(success=True).count()
        current_with_response = current_appeals.exclude(response_date=None).count()

        # Since we're looking for patients in the current range we can't use the UserDomain
        current_patients = (
            PatientUser.objects.filter(appeal__in=current_appeals).distinct().count()
        )

        # Get previous period statistics
        previous_appeals = Appeal.filter_to_allowed_appeals(user).filter(
            creation_date__range=(
                previous_period_start.date(),
                previous_period_end.date(),
            )
        )
        previous_total = previous_appeals.count()
        previous_pending = previous_appeals.filter(pending=True).count()
        previous_sent = previous_appeals.filter(sent=True).count()

        # Use success field instead of response_date
        previous_successful = previous_appeals.filter(success=True).count()
        previous_with_response = previous_appeals.exclude(response_date=None).count()

        # Get previous period patient count
        # Get the patients with appeals in previous period
        previous_patients = (
            PatientUser.objects.filter(appeal__in=previous_appeals).distinct().count()
        )

        # Set estimated payment value to None for now
        current_estimated_payment_value = None
        previous_estimated_payment_value = None

        statistics = {
            "current_total_appeals": current_total,
            "current_pending_appeals": current_pending,
            "current_sent_appeals": current_sent,
            "current_success_rate": (
                (current_successful / current_with_response * 100)
                if current_with_response > 0
                else 0
            ),
            "current_estimated_payment_value": current_estimated_payment_value,
            "current_total_patients": current_patients,
            "previous_total_appeals": previous_total,
            "previous_pending_appeals": previous_pending,
            "previous_sent_appeals": previous_sent,
            "previous_success_rate": (
                (previous_successful / previous_with_response * 100)
                if previous_with_response > 0
                else 0
            ),
            "previous_estimated_payment_value": previous_estimated_payment_value,
            "previous_total_patients": previous_patients,
            "period_start": current_period_start,
            "period_end": current_period_end,
        }

        return Response(serializers.StatisticsSerializer(statistics).data)

    @extend_schema(responses=serializers.AbsoluteStatisticsSerializer)
    @action(detail=False, methods=["get"])
    def absolute_stats(self, request: Request) -> Response:
        """Get absolute statistics without time windowing"""
        user: User = request.user  # type: ignore

        # Get user domain to calculate patients
        domain_id = auth_utils.get_domain_id_from_request(request)
        user_domain = None
        if domain_id:
            try:
                user_domain = UserDomain.objects.get(id=domain_id)
            except UserDomain.DoesNotExist:
                pass

        # Get all appeals the user has access to
        all_appeals = Appeal.filter_to_allowed_appeals(user)
        total_appeals = all_appeals.count()
        pending_appeals = all_appeals.filter(pending=True).count()
        sent_appeals = all_appeals.filter(sent=True).count()

        # Use success field instead of response_date
        successful_appeals = all_appeals.filter(success=True).count()
        with_response = all_appeals.exclude(response_date=None).count()
        success_rate = (
            (successful_appeals / with_response * 100) if with_response > 0 else 0.0
        )

        # Set estimated payment value to None for now
        estimated_payment_value = None

        # Get total patient count based on domain
        total_patients = 0
        if user_domain:
            # Get all patients related to this domain
            total_patients = (
                PatientUser.objects.filter(patientdomainrelation__domain=user_domain)
                .distinct()
                .count()
            )
        else:
            # Fallback to patients with appeals if domain not available
            total_patients = (
                PatientUser.objects.filter(appeal__in=all_appeals).distinct().count()
            )

        statistics = {
            "total_appeals": total_appeals,
            "pending_appeals": pending_appeals,
            "sent_appeals": sent_appeals,
            "success_rate": success_rate,
            "estimated_payment_value": estimated_payment_value,
            "total_patients": total_patients,
        }

        return Response(serializers.AbsoluteStatisticsSerializer(statistics).data)

    @extend_schema(
        responses={
            200: serializers.SearchResultSerializer,
            400: serializers.ErrorSerializer,
        }
    )
    @action(detail=False, methods=["get"])
    def search(self, request):
        query = request.GET.get("q", "")
        if not query:
            return Response(
                serializers.ErrorSerializer(
                    {"error": 'Please provide a search query parameter "q"'}
                ).data,
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Search in Appeals with user permissions
        appeals = Appeal.filter_to_allowed_appeals(request.user).filter(
            Q(uuid__icontains=query)
            | Q(appeal_text__icontains=query)
            | Q(response_text__icontains=query)
        )

        # Convert appeals to search results
        search_results = []
        for appeal in appeals:
            search_results.append(
                {
                    "id": appeal.id,
                    "uuid": appeal.uuid,
                    "appeal_text": (
                        appeal.appeal_text[:200] if appeal.appeal_text else ""
                    ),
                    "pending": appeal.pending,
                    "sent": appeal.sent,
                    "mod_date": appeal.mod_date,
                    "has_response": appeal.response_date is not None,
                }
            )

        # Sort results by modification date (newest first)
        search_results.sort(key=lambda x: x["mod_date"], reverse=True)

        # Paginate results
        page_size = int(request.GET.get("page_size", 10))
        page = int(request.GET.get("page", 1))
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size

        paginated_results = search_results[start_idx:end_idx]

        return Response(
            {
                "count": len(search_results),
                "next": page < len(search_results) // page_size + 1,
                "previous": page > 1,
                "results": paginated_results,
            }
        )


class MailingListSubscriberViewSet(viewsets.ViewSet, CreateMixin, DeleteMixin):
    """
    ViewSet for managing mailing list subscriptions.

    Allows users to subscribe to or unsubscribe from the mailing list by email.
    """

    serializer_class = serializers.MailingListSubscriberSerializer

    @extend_schema(responses=serializers.StatusResponseSerializer)
    def create(self, request: Request) -> Response:
        return super().create(request)

    @extend_schema(responses=serializers.StatusResponseSerializer)
    def perform_create(self, request: Request, serializer) -> Response:
        serializer.save()
        return Response(
            serializers.StatusResponseSerializer({"status": "subscribed"}).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(responses=serializers.StatusResponseSerializer)
    def perform_delete(self, request: Request, serializer):
        email = serializer.validated_data["email"]
        MailingListSubscriber.objects.filter(email=email).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class DemoRequestsViewSet(viewsets.ViewSet, CreateMixin, DeleteMixin):
    """
    ViewSet for managing demo requests.

    Allows users to submit requests for product demonstrations or remove
    existing demo requests by email.
    """

    serializer_class = serializers.DemoRequestsSerializer

    @extend_schema(responses=serializers.StatusResponseSerializer)
    def create(self, request: Request) -> Response:
        return super().create(request)

    @extend_schema(responses=serializers.StatusResponseSerializer)
    def perform_create(self, request: Request, serializer) -> Response:
        serializer.save()
        return Response(
            serializers.StatusResponseSerializer({"status": "subscribed"}).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(responses=serializers.StatusResponseSerializer)
    def perform_delete(self, request: Request, serializer):
        email = serializer.validated_data["email"]
        DemoRequests.objects.filter(email=email).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class SendToUserViewSet(viewsets.ViewSet, SerializerMixin):
    """Send a draft appeal to a user to fill in."""

    serializer_class = serializers.SendToUserSerializer

    @extend_schema(responses=serializers.StatusResponseSerializer)
    def post(self, request):
        current_user: User = request.user  # type: ignore
        serializer = self.deserialize(request.data)
        serializer.is_valid(raise_exception=True)
        # TODO: Send an e-mail to the patient
        appeal = Appeal.filter_to_allowed_appeals(current_user).get(
            id=serializer.validated_data["appeal_id"]
        )


class AppealAttachmentViewSet(viewsets.ViewSet):
    """
    ViewSet for managing file attachments on appeals.

    Supports listing, uploading, downloading (with decryption), and deleting
    attachments. Files are encrypted at rest and decrypted on retrieval.
    """

    serializer_class = serializers.AppealAttachmentSerializer

    @extend_schema(
        responses={
            200: serializers.AppealAttachmentSerializer,
            400: serializers.ErrorSerializer,
        }
    )
    def list(self, request: Request) -> Response:
        """List attachments for a given appeal"""
        appeal_id = request.query_params.get("appeal_id")
        if not appeal_id:
            return Response(
                serializers.ErrorSerializer({"error": "appeal_id required"}).data,
                status=status.HTTP_400_BAD_REQUEST,
            )

        appeal_id_number: Optional[int] = (
            int(appeal_id) if appeal_id.isdigit() else None
        )
        if not appeal_id_number:
            return Response(
                serializers.ErrorSerializer({"error": "Invalid appeal_id"}).data,
                status=status.HTTP_400_BAD_REQUEST,
            )

        current_user: User = request.user  # type: ignore
        appeal = get_object_or_404(
            Appeal.filter_to_allowed_appeals(current_user), id=appeal_id_number
        )
        attachments = AppealAttachment.objects.filter(appeal=appeal)
        serializer = serializers.AppealAttachmentSerializer(attachments, many=True)
        return Response(serializer.data)

    @extend_schema(responses=serializers.AppealAttachmentSerializer)
    def create(self, request: Request) -> Response:
        """Upload a new attachment"""
        serializer = serializers.AppealAttachmentUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        current_user: User = request.user  # type: ignore
        appeal = get_object_or_404(
            Appeal.filter_to_allowed_appeals(current_user),
            id=serializer.validated_data["appeal_id"],
        )

        file = serializer.validated_data["file"]
        attachment = None
        try:
            attachment = AppealAttachment.objects.create(
                appeal=appeal,
                file=file,
                filename=file.name,
                mime_type=file.content_type,
            )
        except SuspiciousFileOperation:
            # Try with random filename, same extension
            from fighthealthinsurance.utils import (
                generate_random_filename_with_extension,
                generate_random_unsupported_filename,
            )

            random_filename = generate_random_filename_with_extension(file.name)
            try:
                attachment = AppealAttachment.objects.create(
                    appeal=appeal,
                    file=file,
                    filename=random_filename,
                    mime_type=file.content_type,
                )
            except SuspiciousFileOperation:
                # Try with .unsupported extension
                fallback_filename = generate_random_unsupported_filename()
                attachment = AppealAttachment.objects.create(
                    appeal=appeal,
                    file=file,
                    filename=fallback_filename,
                    mime_type=file.content_type,
                )
        return Response(
            serializers.AppealAttachmentSerializer(attachment).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(responses=serializers.AppealAttachmentSerializer)
    def retrieve(self, request: Request, pk: int) -> FileResponse:
        """Download an attachment"""
        current_user: User = request.user  # type: ignore
        attachment = get_object_or_404(
            AppealAttachment.filter_to_allowed_attachments(current_user), id=pk
        )
        file = attachment.document_enc.open()
        content = Cryptographer.decrypted(file.read())
        response = FileResponse(
            content,
            content_type=attachment.mime_type,
            as_attachment=True,
            filename=attachment.filename,
        )
        return response

    @extend_schema(responses=serializers.StatusResponseSerializer)
    def destroy(self, request: Request, pk=None) -> Response:
        """Delete an attachment"""
        current_user: User = request.user  # type: ignore
        attachment = get_object_or_404(
            AppealAttachment.filter_to_allowed_attachments(current_user), id=pk
        )
        attachment.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class PriorAuthViewSet(viewsets.ViewSet, SerializerMixin):
    """ViewSet for managing prior authorization requests."""

    def get_serializer_class(self):
        if self.action == "create":
            return serializers.PriorAuthCreateSerializer
        elif self.action == "submit_answers":
            return serializers.PriorAuthAnswersSerializer
        elif self.action == "select_proposal":
            return serializers.PriorAuthSelectSerializer
        elif self.action == "retrieve":
            return serializers.PriorAuthDetailSerializer
        elif self.action == "extract_patient_fields":
            return serializers.ExtractPatientFieldsSerializer
        return serializers.PriorAuthRequestSerializer

    @extend_schema(
        request=serializers.ExtractPatientFieldsSerializer,
        responses={200: serializers.ExtractPatientFieldsResponseSerializer},
    )
    @action(detail=False, methods=["post"])
    def extract_patient_fields(self, request: Request) -> Response:
        """
        Extract patient fields from uploaded PDF text content using ML entity extraction.

        Accepts raw text extracted from a PDF and returns structured field values that can
        be used to prefill a prior auth form.
        """
        # Ensure user is authenticated
        if not request.user.is_authenticated:
            return Response(
                {"error": "Authentication required"},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        # Deserialize and validate the request data
        serializer = serializers.ExtractPatientFieldsSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        # Get the raw text from the request
        text = serializer.validated_data["text"]

        # Get entity extraction backends
        entity_backends = ml_router.entity_extract_backends(use_external=False)

        if not entity_backends:
            return Response(
                {"error": "No entity extraction models available"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        # Get the first available model
        model = entity_backends[0]

        # Call entity extraction asynchronously for each field
        async def extract_fields():
            """Extract all required fields from the text asynchronously."""
            # Create tasks for parallel execution
            tasks = [
                model.get_entity(text, "patient_name"),
                model.get_entity(text, "member_id"),
                model.get_entity(text, "date_of_birth"),
                model.get_entity(text, "plan_id"),
                model.get_entity(text, "insurance_company"),
                # Remove diagnosis as it's not available from patient biographics
            ]

            # Field names corresponding to the tasks
            fields = [
                "patient_name",
                "member_id",
                "dob",
                "plan_id",
                "insurance_company",
            ]

            # Run all extraction tasks in parallel
            results = {}
            extracted_values = await asyncio.gather(*tasks, return_exceptions=True)

            for i, value in enumerate(extracted_values):
                if i < len(fields):  # Safety check
                    field = fields[i]
                    if not isinstance(value, Exception) and value:
                        results[field] = value
                    elif isinstance(value, Exception):
                        logger.error(f"Error extracting {field}: {value}")

            return results

        # Run the extraction and get the results
        try:
            extracted_fields = async_to_sync(extract_fields)()

            # Process date of birth if present
            if "dob" in extracted_fields:
                try:
                    # Try to parse the date from various formats
                    from dateutil import parser

                    dob_str = extracted_fields["dob"]
                    # If parsing fails, the field will remain as string
                    extracted_fields["dob"] = parser.parse(dob_str).date()
                except Exception as e:
                    logger.error(f"Error parsing date of birth: {e}")
                    # Keep the original string if date parsing fails

            # Create a response serializer to validate the data
            response_serializer = serializers.ExtractPatientFieldsResponseSerializer(
                extracted_fields
            )

            # Return the response with the extracted fields
            return Response(response_serializer.data, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"Error in extract_patient_fields: {e}")
            return Response(
                {"error": "Failed to extract patient information"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @extend_schema(responses=serializers.PriorAuthRequestSerializer)
    def create(self, request: Request) -> Response:
        """Create a new prior authorization request and generate initial questions."""
        serializer = self.deserialize(data=request.data)
        serializer.is_valid(raise_exception=True)

        current_user: User = request.user  # type: ignore
        professional_user = get_object_or_404(ProfessionalUser, user=current_user)

        created_for_professional_user_id = serializer.validated_data.get(
            "created_for_professional_user_id"
        )
        logger.debug(
            "Looking up created_for_professional_user_id {}".format(
                created_for_professional_user_id
            )
        )
        created_for_professional_user = None
        if created_for_professional_user_id:
            created_for_professional_user = get_object_or_404(
                ProfessionalUser, id=created_for_professional_user_id
            )

        # Extract domain from session
        domain_id = request.session.get("domain_id")
        domain = None
        if domain_id:
            domain = get_object_or_404(UserDomain, id=domain_id)

        patient_name = None
        if "patient_name" in serializer.validated_data:
            patient_name = serializer.validated_data["patient_name"]

        # Create the prior auth request
        prior_auth = PriorAuthRequest.objects.create(
            creator_professional_user=professional_user,
            created_for_professional_user=created_for_professional_user,
            diagnosis=serializer.validated_data["diagnosis"],
            treatment=serializer.validated_data["treatment"],
            insurance_company=serializer.validated_data["insurance_company"],
            patient_name=patient_name,
            patient_health_history=serializer.validated_data.get(
                "patient_health_history", ""
            ),
            domain=domain,
            mode=serializer.validated_data.get("mode", "guided"),
            status="initial",
            urgent=serializer.validated_data.get("urgent", False),
            member_id=serializer.validated_data.get("member_id", ""),
            patient_dob=serializer.validated_data.get("patient_dob", None),
            proposal_type=serializer.validated_data.get("proposal_type", "letter"),
        )

        prior_auth.save()

        prior_auth = async_to_sync(self._generate_questions)(prior_auth)

        # Return the response immediately with status questions_asked

        response_data = serializers.PriorAuthRequestSerializer(prior_auth).data
        return Response(response_data, status=status.HTTP_201_CREATED)

    async def _generate_questions(
        self, prior_auth: PriorAuthRequest
    ) -> PriorAuthRequest:
        """
        Generate questions for the prior authorization request using MLAppealQuestionsHelper.
        This runs asynchronously after the initial response is sent.

        For 'raw' mode: generates a single question asking for patient health history
        For 'guided' mode: generates multiple structured questions based on diagnosis and treatment
        """
        try:
            from fighthealthinsurance.ml.ml_appeal_questions_helper import (
                MLAppealQuestionsHelper,
            )

            # Handle based on the selected mode
            if prior_auth.mode == "raw":
                # For raw mode, just provide a single generic question for patient history
                questions = [
                    (
                        "Please provide the patient's complete health history relevant to this prior authorization request:",
                        prior_auth.patient_health_history or "",
                    )
                ]
            else:
                # For guided mode, generate structured questions
                questions = await MLAppealQuestionsHelper.generate_generic_questions(
                    procedure=prior_auth.treatment,
                    diagnosis=prior_auth.diagnosis,
                    timeout=90,
                )
                if questions is None:
                    questions = []
                questions.append(
                    (
                        "Please provide any additional health history relevant to this prior authorization request:",
                        prior_auth.patient_health_history or "",
                    ),
                )

                # Also generate specific questions if health history is provided
                if prior_auth.patient_health_history:
                    specific_questions = (
                        await MLAppealQuestionsHelper.generate_specific_questions(
                            denial_text=None,
                            patient_context=prior_auth.patient_health_history,
                            procedure=prior_auth.treatment,
                            diagnosis=prior_auth.diagnosis,
                            timeout=90,
                            use_external=False,
                        )
                    )

                    # Combine questions, removing duplicates
                    existing_questions = {q[0] for q in questions}
                    for q in specific_questions:
                        if q[0] not in existing_questions:
                            questions.append(q)

            # Update the prior auth with the generated questions
            prior_auth.questions = questions
            prior_auth.status = "questions_asked"
            await prior_auth.asave()
            logger.info(
                f"Generated {len(questions)} questions for prior auth {prior_auth.id}"
            )
            return prior_auth
        except Exception as e:
            logger.opt(exception=True).error(
                f"Error generating questions for prior auth {prior_auth.id}: {e}"
            )
            return prior_auth

    @extend_schema(responses=serializers.PriorAuthRequestSerializer)
    @action(detail=True, methods=["post"])
    def submit_answers(self, request: Request, pk=None) -> Response:
        """Submit answers to the questions for a prior authorization request."""
        current_user: User = request.user  # type: ignore
        prior_auth = get_object_or_404(
            PriorAuthRequest.filter_to_allowed_requests(current_user), id=pk
        )
        serializer = self.deserialize(data=request.data)
        try:
            serializer.is_valid(raise_exception=True)
        except ValidationError as e:
            logger.error(f"Validation error: {e}")
            return Response(
                {"error": "Invalid data", "details": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        answers = None
        if "answers" in serializer.validated_data:
            answers = serializer.validated_data["answers"]

        # Verify token
        token = serializer.validated_data["token"]
        if str(prior_auth.token) != str(token):
            return Response(
                {"error": "Invalid token"}, status=status.HTTP_403_FORBIDDEN
            )

        # Save answers and update status
        prior_auth.answers = serializer.validated_data["answers"]
        prior_auth.status = "questions_answered"
        prior_auth.save()

        return Response(
            serializers.PriorAuthRequestSerializer(prior_auth).data,
            status=status.HTTP_200_OK,
        )

    @extend_schema(responses=serializers.SuccessSerializer)
    @action(detail=True, methods=["post"])
    def select_proposal(self, request: Request, pk=None) -> Response:
        """Select a proposed prior authorization as the final version."""
        current_user: User = request.user  # type: ignore
        prior_auth = get_object_or_404(
            PriorAuthRequest.filter_to_allowed_requests(current_user), id=pk
        )
        serializer = self.deserialize(data=request.data)
        serializer.is_valid(raise_exception=True)

        user_text = None
        if "user_text" in serializer.validated_data:
            user_text = serializer.validated_data["user_text"]

        # Verify token
        token = serializer.validated_data["token"]
        if str(prior_auth.token) != str(token):
            return Response(
                {"error": "Invalid token"}, status=status.HTTP_403_FORBIDDEN
            )

        # Find the proposal
        proposed_id = serializer.validated_data["proposed_id"]
        proposal = get_object_or_404(
            ProposedPriorAuth, proposed_id=proposed_id, prior_auth_request=prior_auth
        )

        # Mark as selected and update status
        ProposedPriorAuth.objects.filter(prior_auth_request=prior_auth).update(
            selected=False
        )
        proposal.selected = True
        proposal.save()

        # Determine which text to use (user-edited or original proposal)
        text_to_use = user_text or proposal.text

        # Substitute patient and provider information into the text
        from fighthealthinsurance.prior_auth_utils import PriorAuthTextSubstituter

        substituted_text = (
            PriorAuthTextSubstituter.substitute_patient_and_provider_info(
                prior_auth, text_to_use
            )
        )

        # Save the substituted text
        prior_auth.text = substituted_text
        prior_auth.status = "completed"
        prior_auth.save()

        return Response(
            serializers.SuccessSerializer(
                {"message": "Prior authorization proposal selected successfully"}
            ).data,
            status=status.HTTP_200_OK,
        )

    @extend_schema(responses=serializers.PriorAuthDetailSerializer)
    def retrieve(self, request: Request, pk=None) -> Response:
        """Retrieve a detailed view of a specific prior authorization request."""
        current_user: User = request.user  # type: ignore
        prior_auth = get_object_or_404(
            PriorAuthRequest.filter_to_allowed_requests(current_user), id=pk
        )

        return Response(
            serializers.PriorAuthDetailSerializer(prior_auth).data,
            status=status.HTTP_200_OK,
        )

    @extend_schema(responses=serializers.PriorAuthRequestSerializer(many=True))
    def list(self, request: Request) -> Response:
        """List all prior authorization requests accessible to the current user."""
        current_user: User = request.user  # type: ignore
        prior_auths = PriorAuthRequest.filter_to_allowed_requests(current_user)

        # Basic filtering options
        status_filter = request.query_params.get("status")
        if status_filter:
            prior_auths = prior_auths.filter(status=status_filter)

        # Sorting
        sort_by = request.query_params.get("sort_by", "-created_at")
        prior_auths = prior_auths.order_by(sort_by)

        return Response(
            serializers.PriorAuthRequestSerializer(prior_auths, many=True).data,
            status=status.HTTP_200_OK,
        )


class ProposedPriorAuthViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet for viewing proposed prior authorizations."""

    serializer_class = serializers.ProposedPriorAuthSerializer

    def get_queryset(self):
        current_user: User = self.request.user  # type: ignore

        # Get the prior auth ID from the URL
        prior_auth_id = self.kwargs.get("prior_auth_id")
        if not prior_auth_id:
            return ProposedPriorAuth.objects.none()

        # Check if the user has access to the related prior auth
        prior_auth = get_object_or_404(
            PriorAuthRequest.filter_to_allowed_requests(current_user), id=prior_auth_id
        )

        # Return proposals for this prior auth
        return ProposedPriorAuth.objects.filter(prior_auth_request=prior_auth)


class ChooserViewSet(viewsets.ViewSet):
    """
    ViewSet for the Chooser (Best-Of Selection) system.

    Provides endpoints for:
    - GET /api/chooser/next/appeal - Get next appeal chooser task
    - GET /api/chooser/next/chat - Get next chat chooser task
    - POST /api/chooser/vote - Submit a vote for a task
    """

    # Allow unauthenticated access and disable CSRF checks
    # This allows both logged-in and anonymous users to vote
    permission_classes = [AllowAny]
    authentication_classes = []

    def _get_session_key(self, request: Request) -> str:
        """Get or create a session key for the request."""
        if not request.session.session_key:
            request.session.create()
        session_key = request.session.session_key
        if not session_key:
            # Check if we already have a fallback session key stored
            fallback_key = request.session.get("_fallback_session_key")
            if fallback_key:
                return str(fallback_key)
            # Generate a new fallback session key and persist it
            import uuid

            fallback_key = f"fallback-{uuid.uuid4().hex}"
            request.session["_fallback_session_key"] = fallback_key
            request.session.save()
            session_key = fallback_key
        return session_key

    def _get_next_task(self, request: Request, task_type: str) -> Response:
        """
        Get the next available chooser task for the given type.

        Selection logic:
        1. Prefer tasks with zero votes
        2. If none, select tasks with the fewest total votes
        3. Exclude tasks where this session key has already voted or skipped
        """
        session_key = self._get_session_key(request)

        # Get tasks that this session has already voted on or skipped
        voted_task_ids = ChooserVote.objects.filter(
            session_key=session_key
        ).values_list("task_id", flat=True)
        skipped_task_ids = ChooserSkip.objects.filter(
            session_key=session_key
        ).values_list("task_id", flat=True)
        excluded_task_ids = set(voted_task_ids) | set(skipped_task_ids)

        # Find READY tasks of the requested type that haven't been voted on or skipped
        available_tasks = (
            ChooserTask.objects.filter(task_type=task_type, status="READY")
            .exclude(id__in=excluded_task_ids)
            .annotate(vote_count=Count("votes"))
            .order_by("vote_count", "created_at")
        )

        task = available_tasks.first()

        if not task:
            # Generate a single task synchronously (blocking) since nothing is available
            from asgiref.sync import async_to_sync

            from fighthealthinsurance.chooser_tasks import _generate_single_task

            try:
                # Generate one task immediately for this request (blocking call)
                async_to_sync(_generate_single_task)(task_type)
            except Exception as e:
                logger.warning(f"Failed to generate task on demand: {e}")
                return Response(
                    {"message": "No tasks available", "task_type": task_type},
                    status=status.HTTP_404_NOT_FOUND,
                )

            # Try to get the task again
            task = (
                ChooserTask.objects.filter(task_type=task_type, status="READY")
                .exclude(id__in=excluded_task_ids)
                .first()
            )

            if not task:
                return Response(
                    {"message": "No tasks available", "task_type": task_type},
                    status=status.HTTP_404_NOT_FOUND,
                )

        # Get candidates for this task
        candidates = ChooserCandidate.objects.filter(
            task=task, is_active=True
        ).order_by("candidate_index")

        if not candidates.exists():
            return Response(
                {"message": "Task has no candidates", "task_type": task_type},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Build context from the task
        context = {}
        if task.context_json:
            context = task.context_json

        # Serialize candidates
        candidate_data = [
            {
                "id": c.id,
                "candidate_index": c.candidate_index,
                "kind": c.kind,
                "model_name": c.model_name,
                "content": c.content,
                "metadata": c.metadata,
            }
            for c in candidates
        ]

        response_data = {
            "task_id": task.id,
            "task_type": task.task_type,
            "task_context": context,
            "candidates": candidate_data,
        }

        return Response(
            serializers.ChooserTaskSerializer(response_data).data,
            status=status.HTTP_200_OK,
        )

    @extend_schema(responses=serializers.ChooserTaskSerializer)
    @action(detail=False, methods=["get"], url_path="next/appeal")
    def next_appeal(self, request: Request) -> Response:
        """Get the next appeal letter chooser task."""
        return self._get_next_task(request, "appeal")

    @extend_schema(responses=serializers.ChooserTaskSerializer)
    @action(detail=False, methods=["get"], url_path="next/chat")
    def next_chat(self, request: Request) -> Response:
        """Get the next chat response chooser task."""
        return self._get_next_task(request, "chat")

    @extend_schema(
        request=serializers.ChooserVoteRequestSerializer,
        responses={
            200: serializers.ChooserVoteResponseSerializer,
            400: serializers.ErrorSerializer,
            404: serializers.ErrorSerializer,
        },
    )
    @action(detail=False, methods=["post"])
    def vote(self, request: Request) -> Response:
        """
        Register a user's vote for a chooser task.

        Validates the task exists and is in a votable state, the chosen candidate belongs to the task and was presented, and the session has not already voted on the task; then creates a ChooserVote.

        Returns:
            response (Response): On success, a 200 response containing {"success": True, "message": "Vote recorded successfully", "vote_id": <id>}. On failure, a 4xx response with an error message.
        """
        serializer = serializers.ChooserVoteRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                serializers.ErrorSerializer({"error": str(serializer.errors)}).data,
                status=status.HTTP_400_BAD_REQUEST,
            )

        session_key = self._get_session_key(request)
        task_id = serializer.validated_data["task_id"]
        chosen_candidate_id = serializer.validated_data["chosen_candidate_id"]
        presented_candidate_ids = serializer.validated_data["presented_candidate_ids"]

        # Validate task exists and is in valid state
        try:
            task = ChooserTask.objects.get(id=task_id)
        except ChooserTask.DoesNotExist:
            return Response(
                serializers.ErrorSerializer({"error": "Task not found"}).data,
                status=status.HTTP_404_NOT_FOUND,
            )

        if task.status not in ["READY", "IN_USE"]:
            return Response(
                serializers.ErrorSerializer(
                    {"error": "Task is not available for voting"}
                ).data,
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Validate chosen candidate exists and belongs to this task
        try:
            chosen_candidate = ChooserCandidate.objects.get(
                id=chosen_candidate_id, task=task
            )
        except ChooserCandidate.DoesNotExist:
            return Response(
                serializers.ErrorSerializer(
                    {"error": "Invalid candidate for this task"}
                ).data,
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Check if this session has already voted on this task
        if ChooserVote.objects.filter(task=task, session_key=session_key).exists():
            return Response(
                serializers.ErrorSerializer(
                    {"error": "You have already voted on this task"}
                ).data,
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Validate that chosen_candidate_id is in presented_candidate_ids
        if chosen_candidate_id not in presented_candidate_ids:
            return Response(
                serializers.ErrorSerializer(
                    {"error": "Chosen candidate was not in the presented candidates"}
                ).data,
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Create the vote
        try:
            vote = ChooserVote.objects.create(
                task=task,
                chosen_candidate=chosen_candidate,
                presented_candidate_ids=presented_candidate_ids,
                session_key=session_key,
            )
        except IntegrityError:
            return Response(
                serializers.ErrorSerializer(
                    {"error": "You have already voted on this task"}
                ).data,
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            serializers.ChooserVoteResponseSerializer(
                {
                    "success": True,
                    "message": "Vote recorded successfully",
                    "vote_id": vote.id,
                }
            ).data,
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        request=serializers.ChooserSkipRequestSerializer,
        responses={
            200: serializers.ChooserSkipResponseSerializer,
            400: serializers.ErrorSerializer,
            404: serializers.ErrorSerializer,
        },
    )
    @action(detail=False, methods=["post"])
    def skip(self, request: Request) -> Response:
        """
        Mark a chooser task as skipped for the current session so it will not be presented again.

        Validates the request payload and session, ensures the task exists, prevents skipping if the session has already voted on the task, and records the skip idempotently.

        Returns:
            Response: HTTP 200 with a success message when the task is skipped; HTTP 400 with error details for invalid input or if the task was already voted on; HTTP 404 if the task does not exist.
        """
        serializer = serializers.ChooserSkipRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                serializers.ErrorSerializer({"error": str(serializer.errors)}).data,
                status=status.HTTP_400_BAD_REQUEST,
            )

        session_key = self._get_session_key(request)
        task_id = serializer.validated_data["task_id"]

        # Validate task exists
        try:
            task = ChooserTask.objects.get(id=task_id)
        except ChooserTask.DoesNotExist:
            return Response(
                serializers.ErrorSerializer({"error": "Task not found"}).data,
                status=status.HTTP_404_NOT_FOUND,
            )

        # Check if already voted on this task (can't skip if already voted)
        if ChooserVote.objects.filter(task=task, session_key=session_key).exists():
            return Response(
                serializers.ErrorSerializer(
                    {"error": "You have already voted on this task"}
                ).data,
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Create the skip record (or ignore if already skipped)
        ChooserSkip.objects.get_or_create(
            task=task,
            session_key=session_key,
        )

        return Response(
            serializers.ChooserSkipResponseSerializer(
                {
                    "success": True,
                    "message": "Task skipped successfully",
                }
            ).data,
            status=status.HTTP_200_OK,
        )
