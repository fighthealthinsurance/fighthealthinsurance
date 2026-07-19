import asyncio
import datetime
import json
import typing
from typing import Optional

from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation
from django.core.mail import send_mail
from django.db import IntegrityError, models
from django.db.models import Count, Q
from django.http import FileResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.shortcuts import get_object_or_404
from django.utils import timezone

from asgiref.sync import async_to_sync, sync_to_async
from dateutil.relativedelta import relativedelta
from django_encrypted_filefield.crypt import Cryptographer
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from loguru import logger
from rest_framework import status, viewsets
from rest_framework.decorators import (
    action,
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from stopit import ThreadingTimeout as Timeout

from fhi_users.auth import auth_utils
from fhi_users.models import PatientUser, ProfessionalUser, UserDomain
from fighthealthinsurance import (
    common_view_logic,
    context_utils,
    rest_serializers as serializers,
)
from fighthealthinsurance.denial_context import merge_qa
from fighthealthinsurance.external_review import (
    generate_external_review_packet,
    schedule_external_review_followups,
)
from fighthealthinsurance.helpers.fax_helpers import SendFaxHelper
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
    InterestedProfessional,
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
from fighthealthinsurance.type_utils import User
from fighthealthinsurance.websockets import log_zero_appeal_diagnostics

from .common_view_logic import AppealAssemblyHelper
from .utils import is_convertible_to_int, is_valid_denial_id

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


class HealthHistoryViewSet(viewsets.ViewSet, CreateMixin):
    """
    ViewSet for updating patient health history on a denial.

    Accepts health context information and updates the associated denial record.
    """

    serializer_class = serializers.HealthHistoryFormSerializer

    @extend_schema(responses=serializers.StatusResponseSerializer)
    def create(self, request: Request) -> Response:
        """Create or update health history for a denial."""
        return super().create(request)

    @extend_schema(responses=serializers.StatusResponseSerializer)
    def perform_create(self, request: Request, serializer) -> Response:
        """Update the denial record with provided health history context."""
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
        """Analyze denial data and return recommended next steps."""
        return super().create(request)

    def perform_create(self, request: Request, serializer) -> Response:
        """Process denial info and return appeal options and regulatory guidance."""
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
        """Create a new denial record or update an existing one."""
        return super().create(request)

    @extend_schema(responses=serializers.DenialResponseInfoSerializer)
    def retrieve(self, request: Request, pk: int) -> Response:
        """Retrieve detailed information about a specific denial."""
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
        """Process denial creation with professional associations and subscriptions."""
        current_user: User = request.user  # type: ignore
        creating_professional = ProfessionalUser.objects.get(user=current_user)
        serializer = self.deserialize(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer_data = serializer.validated_data
        logger.debug(
            f"perform_create payload: keys={sorted(serializer_data.keys())} "
            f"has_denial_id={bool(serializer_data.get('denial_id'))}"
        )
        session_key = request.session.session_key or "no_session_key"
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
            if denial_id and is_valid_denial_id(denial_id):
                denial_id = int(denial_id)
                denial = Denial.filter_to_allowed_denials(current_user).get(
                    denial_id=denial_id
                )
            elif denial_id and denial_id != "":
                logger.warning(
                    "Invalid denial_id format during denial create/update. "
                    f"user_id={current_user.id} session_key={session_key} "
                    f"remote_ip={request.META.get('REMOTE_ADDR', 'unknown')} "
                    f"denial_id={denial_id}"
                )
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
        if (
            str(serializer_data.get("email", "")).strip().lower()
            == "testing@example.com"
        ):
            from fhi_users.audit import get_client_ip

            tracking_info.ip_address = get_client_ip(request)

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
        if not is_valid_denial_id(denial_response_info.denial_id):
            logger.error(
                "Invalid denial_id in denial create response. "
                f"user_id={current_user.id} session_key={session_key} "
                f"denial_uuid={denial_response_info.uuid} "
                f"denial_id={denial_response_info.denial_id}"
            )
            raise ValidationError("Invalid denial_id generated while creating denial")
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
        """Store Q&A responses for a denial."""
        return super().create(request)

    @extend_schema(responses=serializers.StatusResponseSerializer)
    def perform_create(self, request: Request, serializer) -> Response:
        """Bulk save Q&A responses and update denial context."""
        user: User = request.user  # type: ignore
        denial = Denial.filter_to_allowed_denials(user).get(
            denial_id=serializer.validated_data["denial_id"]
        )
        # Fetch all existing DenialQA objects for this denial in a single query (N+1 fix)
        existing_qa = {
            dqa.question: dqa for dqa in DenialQA.objects.filter(denial=denial)
        }
        to_update = []
        to_create = []
        for key, value in serializer.validated_data["qa"].items():
            if not key or not value or len(value) == 0:
                continue
            if key in existing_qa:
                dqa = existing_qa[key]
                dqa.text_answer = value
                to_update.append(dqa)
            else:
                to_create.append(
                    DenialQA(denial=denial, question=key, text_answer=value)
                )
        # Bulk update and create to minimize database round-trips
        if to_update:
            DenialQA.objects.bulk_update(to_update, ["text_answer"])
        if to_create:
            DenialQA.objects.bulk_create(to_create)
        # Merge the DenialQA rows into qa_context instead of rebuilding it
        # from scratch — rebuilding clobbers keys like "medical_context" or
        # form-derived dates that other writers stored.
        merged_updates = {dqa.question: dqa.text_answer for dqa in existing_qa.values()}
        merged_updates.update({dqa.question: dqa.text_answer for dqa in to_create})
        merge_qa(denial, merged_updates, source="rest_qa_response")
        denial.save(update_fields=["qa_context"])
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
        """Record follow-up outcome for an appeal."""
        return super().create(request)

    @extend_schema(responses=serializers.StatusResponseSerializer)
    def perform_create(self, request: Request, serializer) -> Response:
        """Store the user-reported appeal result for tracking."""
        common_view_logic.FollowUpHelper.store_follow_up_result(
            **serializer.validated_data
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class ReportClientError(APIView):
    """Endpoint for clients to report appeal-related errors server-side for Sentry visibility."""

    permission_classes = [AllowAny]
    authentication_classes = []  # No auth needed; also avoids CSRF rejection

    def post(self, request: Request) -> Response:
        def _sanitize(val: str, max_len: int) -> str:
            return val[:max_len].replace("\r", " ").replace("\n", " ")

        # Sanitize inputs: truncate and strip CR/LF to prevent log injection
        denial_id = _sanitize(str(request.data.get("denial_id", "unknown")), 200)
        denial_id_raw = _sanitize(str(request.data.get("denial_id_raw", "")), 200)
        error_message = _sanitize(str(request.data.get("error", "unknown error")), 500)
        browser_info = _sanitize(str(request.data.get("browser_info", "")), 500)
        # Delivery diagnostics are client-collected counters (status frames,
        # ws close code/reason, server-reported totals, etc). They share the
        # same sanitization rules but get their own field so a long mobile
        # user-agent string in browser_info doesn't crowd them out.
        diagnostics = _sanitize(str(request.data.get("diagnostics", "")), 1000)
        session_key = request.session.session_key or "no_session_key"
        denial_id_valid = is_valid_denial_id(denial_id)
        # Annotate with the (estimated) token sizes of the denial text and the
        # context that feeds appeal generation. A client-reported "0 appeals"
        # is frequently a context-window overflow, and seeing the per-field
        # token breakdown server-side makes that diagnosable without a repro.
        #
        # This enrichment reads PHI-bearing denial fields, so it is gated on the
        # same (denial_id, email, semi_sekret) ownership triple that guards the
        # appeal stream -- otherwise any sequential denial_id would load an
        # arbitrary patient's denial (unauthenticated IDOR). Auth failure still
        # records the error report: this endpoint is a fire-and-forget
        # diagnostic sink that must work even in degraded client states, so we
        # log without the token breakdown rather than reject. Never let the
        # enrichment break the report.
        context_tokens = "unauthorized"
        # Wrap BOTH the ownership lookup and the summarize call: this endpoint's
        # whole contract is "always 204, never let the diagnostic enrichment
        # break the report", and get_denial_for_action does a DB query that can
        # raise. Keeping it outside the try would let a DB/runtime error there
        # escape and 500 the error reporter.
        try:
            denial = common_view_logic.get_denial_for_action(
                denial_id=request.data.get("denial_id"),
                email=str(request.data.get("email") or ""),
                semi_sekret=str(request.data.get("semi_sekret") or ""),
            )
            if denial is not None:
                context_tokens = context_utils.summarize_denial_context_tokens(denial)
        except Exception as e:
            context_tokens = "unavailable"
            logger.opt(exception=True).debug(
                f"Failed to compute context token sizes for denial " f"{denial_id}: {e}"
            )
        logger.error(
            f"Client-reported appeal error for denial {denial_id}: "
            f"{error_message} | browser: {browser_info} | "
            f"diagnostics: {diagnostics} | "
            f"context_tokens: {context_tokens} | "
            f"session_key={session_key} | remote_ip={request.META.get('REMOTE_ADDR', 'unknown')} | "
            f"denial_id_valid={denial_id_valid} | denial_id_raw={denial_id_raw}"
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class EnableExternalModels(APIView):
    """Opt a denial into external models so a re-run of appeal generation
    can call out to higher-capability cloud LLMs.

    Patients see a button on the appeals page offering this when the
    initial generation produced few or no appeals from internal models;
    this endpoint flips `Denial.use_external` so the next generation
    request includes the external backends. Auth mirrors the appeal
    stream: the (denial_id, email, semi_sekret) triple is the only
    credential.
    """

    permission_classes = [AllowAny]
    authentication_classes = []  # Magic-key auth via the triple

    @extend_schema(
        description=(
            "Opt a denial into external models. Authenticated by the "
            "(denial_id, email, semi_sekret) triple in the JSON body. "
            "Idempotent — succeeds with 200 if `use_external` is "
            "already True. Returns 404 for any auth failure (uniform "
            "to avoid leaking which field was wrong)."
        ),
        request=OpenApiTypes.OBJECT,
        responses={
            200: OpenApiTypes.OBJECT,
            400: OpenApiTypes.OBJECT,
            404: OpenApiTypes.OBJECT,
        },
    )
    def post(self, request: Request) -> Response:
        data = request.data
        # Reject non-mapping bodies (e.g. JSON arrays/strings) up front;
        # without this, request.data.get(...) raises and we'd serve 500
        # on malformed input. Matches streaming_appeals_rest_fallback.
        if not isinstance(data, dict):
            return Response(
                {"error": "Expected a JSON object"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        denial = common_view_logic.get_denial_for_action(
            denial_id=data.get("denial_id"),
            email=data.get("email") or "",
            semi_sekret=data.get("semi_sekret") or "",
        )
        if denial is None:
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)
        if not denial.use_external:
            denial.use_external = True
            denial.save(update_fields=["use_external"])
            logger.info(
                f"Enabled external models for denial {denial.denial_id} on user request"
            )
        return Response({"use_external": True}, status=status.HTTP_200_OK)


@extend_schema(
    description=(
        "REST/HTTP fallback for the WebSocket appeal stream used by "
        "clients that can't hold a WebSocket open long enough (iOS "
        "Safari ITP, corporate proxies blocking WS upgrades, captive "
        "portals). Auth model mirrors the WebSocket: the "
        "(denial_id, email, semi_sekret) triple inside the body is "
        "the only credential. Response body is NDJSON: one JSON "
        "object per line — either a status frame "
        '`{type: "status", phase, message}`, an appeal payload '
        "`{id, content}`, or an error frame "
        '`{type: "error", message}`.'
    ),
    request=OpenApiTypes.OBJECT,
    responses={
        200: OpenApiTypes.STR,
        400: OpenApiTypes.OBJECT,
    },
)
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@csrf_exempt
def streaming_appeals_rest_fallback(request: Request):
    """REST/HTTP fallback for the WebSocket appeal stream.

    The view is sync but returns a `StreamingHttpResponse` whose
    `streaming_content` is an async generator. Django 4.2+ on ASGI
    drives the async iterator directly, preserving true streaming
    semantics — the client sees each frame as the underlying
    `AppealsBackendHelper.generate_appeals` async generator yields it.
    """
    try:
        data = request.data
    except Exception as e:
        logger.warning(f"Invalid request body in REST appeals fallback: {e}")
        return Response(
            {"error": "Invalid JSON format"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not isinstance(data, dict):
        logger.warning(
            f"REST appeals fallback got non-object JSON: {type(data).__name__}"
        )
        return Response(
            {"error": "Expected a JSON object"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    denial_id = data.get("denial_id")
    # Magic-key auth gate: verify (denial_id, email, semi_sekret)
    # triple resolves to a real Denial BEFORE starting the stream.
    # Without this, malformed/wrong credentials would burn ML
    # generation cycles and return 200 + an in-band error frame,
    # which is harder for clients to handle deterministically.
    # Uniform 404 for any auth failure so we don't leak which field
    # was wrong.
    denial = common_view_logic.get_denial_for_action(
        denial_id=denial_id,
        email=data.get("email") or "",
        semi_sekret=data.get("semi_sekret") or "",
    )
    if denial is None:
        logger.warning(f"REST appeals fallback auth failure for denial {denial_id!r}")
        return Response(
            {"error": "Not found"},
            status=status.HTTP_404_NOT_FOUND,
        )

    async def stream():
        appeal_count = 0
        status_count = 0
        last_status_phase: Optional[str] = None
        try:
            # Flush a leading newline before awaiting generate_appeals
            # so anti-buffering headers and intermediary heuristics
            # engage immediately. Otherwise a slow first ML call can
            # make the fallback look hung even when it's working.
            yield "\n"
            async for record in common_view_logic.AppealsBackendHelper.generate_appeals(
                data
            ):
                # Mirror the WebSocket framing: each record is already
                # newline-terminated JSON, but we add an extra "\n"
                # framing newline so intermediaries that buffer per-line
                # still flush early, matching the WS keepalive cadence.
                yield record
                yield "\n"
                stripped = record.strip()
                if stripped:
                    try:
                        parsed = json.loads(stripped)
                        if isinstance(parsed, dict):
                            if "content" in parsed:
                                appeal_count += 1
                            elif parsed.get("type") == "status":
                                status_count += 1
                                last_status_phase = parsed.get("phase")
                    except (json.JSONDecodeError, TypeError):
                        pass
            if appeal_count == 0:
                await log_zero_appeal_diagnostics(
                    denial_id=denial_id,
                    status_count=status_count,
                    last_status_phase=last_status_phase,
                    transport="rest",
                )
        except Exception as e:
            logger.opt(exception=True).error(
                f"Error streaming REST fallback appeals for denial "
                f"{denial_id} after {appeal_count} appeals / "
                f"{status_count} status frames (last phase="
                f"{last_status_phase}): {e}"
            )
            if appeal_count == 0:
                await log_zero_appeal_diagnostics(
                    denial_id=denial_id,
                    status_count=status_count,
                    last_status_phase=last_status_phase,
                    transport="rest",
                    stream_error=str(e),
                )
            # Inform the client rather than terminating silently
            yield (
                json.dumps(
                    {
                        "type": "error",
                        "message": "Server error while generating appeals.",
                    }
                )
                + "\n"
            )

    response = StreamingHttpResponse(
        streaming_content=stream(),
        content_type="application/x-ndjson",
    )
    # Disable nginx/proxy response buffering so each chunk reaches the
    # client as it's produced. Without this, intermediaries hold back
    # the body until generation completes, defeating the fallback.
    response["Cache-Control"] = "no-cache, no-store"
    response["X-Accel-Buffering"] = "no"
    return response


class ExternalReviewWizardView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request: Request) -> Response:
        denial_id = request.data.get("denial_id")
        email = request.data.get("email")
        semi_sekret = request.data.get("semi_sekret")
        if not denial_id or not email or not semi_sekret:
            return Response(
                {"error": "denial_id, email, and semi_sekret required"}, status=400
            )

        hashed_email = Denial.get_hashed_email(email)
        denial = get_object_or_404(
            Denial,
            denial_id=denial_id,
            semi_sekret=semi_sekret,
            hashed_email=hashed_email,
        )
        payload = {
            "state": request.data.get("state"),
            "plan_type": request.data.get("plan_type"),
            "denial_type": request.data.get("denial_type"),
            "appeal_denial_date": request.data.get("appeal_denial_date"),
            "urgent": request.data.get("urgent"),
        }
        packet = generate_external_review_packet(denial, payload)

        deadline_date = timezone.now().date() + datetime.timedelta(days=120)
        schedule_external_review_followups(denial, email, deadline_date)

        return Response(
            {
                "wizard": {
                    "steps": [
                        "upload final internal denial",
                        "confirm state",
                        "confirm plan type",
                        "confirm urgent medical risk",
                        "collect provider letter/supporting docs",
                        "generate checklist and cover letter",
                    ]
                },
                "packet": packet,
            }
        )


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


class ActorHealthStatus(APIView):
    """Return health status of Ray polling actors.

    Shape:
    { "alive_actors": int, "total_actors": int, "details": [{"name": str, "alive": bool, "error": Optional[str]}] }
    """

    @extend_schema(responses=serializers.ActorHealthStatusSerializer)
    def get(self, request: Request) -> Response:
        try:
            from fighthealthinsurance.actor_health_status import check_actor_health

            result = check_actor_health()
            # Always succeed; return 200 with the current status
            response = Response(result, status=status.HTTP_200_OK)
            # Don't cache this as aggressively - 1 minute max
            response["Cache-Control"] = "public, max-age=60"
            return response
        except Exception as e:
            logger.warning(f"ActorHealthStatus failed: {e}")
            # Graceful degradation: return error state
            return Response(
                {
                    "alive_actors": 0,
                    "total_actors": 3,
                    "details": [],
                    "message": str(e),
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
        """List all appeals accessible to the current user with optimized queries."""
        current_user: User = request.user  # type: ignore
        appeals = Appeal.filter_to_allowed_appeals(current_user)
        # Optimize queries to avoid N+1: prefetch related objects used in serializer
        appeals = appeals.select_related(
            "for_denial",
            "primary_professional",
            "creating_professional",
            "patient_user",
            "chat",
        ).prefetch_related(
            "for_denial__denial_type",
        )
        # Parse the filters
        input_serializer = self.deserialize(data=request.data)
        # TODO: Handle the filters
        output_serializer = serializers.AppealSummarySerializer(appeals, many=True)
        return Response(output_serializer.data)

    @extend_schema(responses=serializers.AppealDetailSerializer)
    def retrieve(self, request: Request, pk: int) -> Response:
        """Retrieve detailed information for a specific appeal."""
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
            staged = SendFaxHelper.stage_appeal_as_fax(
                appeal, email=current_user.email, professional=True
            )
            SendFaxHelper.remote_send_fax(
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
        logger.debug("Creating or updating appeal")
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
        """Subscribe an email to the mailing list."""
        return super().create(request)

    @extend_schema(responses=serializers.StatusResponseSerializer)
    def perform_create(self, request: Request, serializer) -> Response:
        """Save the new mailing list subscription."""
        serializer.save()
        return Response(
            serializers.StatusResponseSerializer({"status": "subscribed"}).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(responses=serializers.StatusResponseSerializer)
    def perform_delete(self, request: Request, serializer):
        """Remove an email from the mailing list."""
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
        """Submit a new demo request."""
        return super().create(request)

    @extend_schema(responses=serializers.StatusResponseSerializer)
    def perform_create(self, request: Request, serializer) -> Response:
        """Save the demo request, recording client IP/ASN, then notify sales."""
        from fhi_users.audit import bound_client_ip, get_asn_info, get_client_ip

        ip = get_client_ip(request)
        asn, asn_name = get_asn_info(ip)
        # bound_client_ip: the IP is taken verbatim from the client-controlled
        # X-Forwarded-For header, so store it bounded (DemoRequests.ip_address is
        # a CharField) rather than risk a malformed/over-long value failing the
        # public, unauthenticated demo-request write.
        demo = serializer.save(
            ip_address=bound_client_ip(ip), asn=asn, asn_name=asn_name
        )
        self._notify_demo_request(demo)
        self._record_interested_professional(demo)
        return Response(
            serializers.StatusResponseSerializer({"status": "subscribed"}).data,
            status=status.HTTP_201_CREATED,
        )

    @staticmethod
    def _record_interested_professional(demo: DemoRequests) -> None:
        """Feed the demo request into the interested-professional flow.

        With new Fight Paperwork signups closed (connector agreement), demo
        requests are the professional lead intake, so mirror the web
        /pro_version form: record an InterestedProfessional lead, notify the
        professional-signup inbox, and send the professional_thankyou email to
        the requester. Best-effort: the DemoRequests row is already persisted
        and this public endpoint must not 500 over lead bookkeeping or mail."""
        from fighthealthinsurance.followup_emails import ThankyouEmailSender
        from fighthealthinsurance.utils import notify_interested_professional

        try:
            # Dedup by email: if this address already has a lead, don't create a
            # duplicate InterestedProfessional, re-notify the professional inbox,
            # or re-send the thank-you. The DemoRequests row + the support
            # notification (_notify_demo_request) already captured this repeat
            # submission for the team. Case-insensitive to match how the
            # pro-connector queue collapses duplicates (email__iexact), so
            # Jane@clinic.org resubmitting as jane@clinic.org isn't re-thanked.
            if InterestedProfessional.objects.filter(email__iexact=demo.email).exists():
                return
            interested_pro = InterestedProfessional.objects.create(
                name=demo.name or "",
                email=demo.email,
                business_name=demo.company or "",
                job_title_or_provider_type=demo.role or "",
                phone_number=demo.phone or "",
                comments=f"Demo request (source: {demo.source or 'direct'})",
                clicked_for_paid=False,
            )
            notify_interested_professional(
                interested_pro,
                source="a Fight Paperwork demo request",
                subject=f"New demo request lead #{interested_pro.id}",
            )
            # Send the thank-you right away; dosend() sets
            # thankyou_email_sent=True on success so the batched
            # ThankyouEmailSender won't re-send, and it swallows mail errors.
            ThankyouEmailSender().dosend(interested_pro=interested_pro)
        except Exception:
            logger.opt(exception=True).error(
                f"Error recording interested professional for demo request {demo.id}"
            )

    @staticmethod
    def _notify_demo_request(demo: DemoRequests) -> None:
        """Email support42@ (plus any DEMO_REQUEST_EXTRA_NOTIFICATION_EMAILS)
        about a new demo request. Best-effort: a mail failure must not fail the
        request, which is already persisted."""
        recipients = list(
            getattr(
                settings,
                "DEMO_REQUEST_NOTIFICATION_EMAILS",
                ["support42@fighthealthinsurance.com"],
            )
        )
        if not recipients:
            return
        body = (
            "New demo request:\n\n"
            f"Email: {demo.email}\n"
            f"Name: {demo.name or 'N/A'}\n"
            f"Company: {demo.company or 'N/A'}\n"
            f"Role: {demo.role or 'N/A'}\n"
            f"Phone: {demo.phone or 'N/A'}\n"
            f"Source: {demo.source or 'N/A'}\n"
            f"IP: {demo.ip_address or 'N/A'}\n"
            f"ASN: {demo.asn or 'N/A'} ({demo.asn_name or 'N/A'})\n"
        )
        try:
            send_mail(
                f"New demo request: {demo.email}",
                body,
                settings.DEFAULT_FROM_EMAIL,
                recipients,
            )
        except Exception:
            logger.opt(exception=True).error(
                f"Error sending demo request notification for {demo.email}"
            )

    @extend_schema(responses=serializers.StatusResponseSerializer)
    def perform_delete(self, request: Request, serializer):
        email = serializer.validated_data["email"]
        DemoRequests.objects.filter(email=email).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class InterestedProfessionalViewSet(viewsets.ViewSet, CreateMixin):
    """
    ViewSet for professional-interest leads submitted via the Fight Paperwork
    REST API.

    Public counterpart to the web /pro_version interest form: it records an
    InterestedProfessional lead and notifies the professional-signup inboxes
    (default to support42@fighthealthinsurance.com and
    professional@fighthealthinsurance.com, extendable via
    settings.PROFESSIONAL_SIGNUP_NOTIFICATION_EMAILS). Create-only: this is a
    public, unauthenticated endpoint, so lead removal is intentionally not
    exposed here (an email-only delete would let anyone erase a lead); data
    removal is handled out of band.
    """

    serializer_class = serializers.InterestedProfessionalSerializer

    @extend_schema(responses=serializers.StatusResponseSerializer)
    def create(self, request: Request) -> Response:
        """Submit a new professional-interest lead."""
        return super().create(request)

    def perform_create(self, request: Request, serializer) -> Response:
        """Save the lead, then notify the professional-signup inbox."""
        # Dedup by email: a returning lead reuses the existing record rather
        # than accumulating duplicate rows (case-insensitive to match how the
        # pro-connector queue collapses duplicates, email__iexact). The team
        # notification still fires — flagged "(returning)" — so repeat
        # interest is never silently swallowed. Repeat submissions get the
        # standard success response either way.
        email = serializer.validated_data.get("email")
        existing = (
            InterestedProfessional.objects.filter(email__iexact=email).first()
            if email
            else None
        )
        if existing is not None:
            # Notify with the freshly submitted values (a new phone number or
            # comment is the interesting part), borrowing the stored row's pk
            # only so the admin deep-link resolves. The transient instance is
            # never saved — matching the web-form returning path — so the
            # stored lead is not overwritten. The shared cooldown gate bounds
            # inbox amplification across every intake path.
            from fighthealthinsurance.utils import should_notify_returning_lead

            if should_notify_returning_lead(existing.email):
                fresh = InterestedProfessional(**serializer.validated_data)
                fresh.id = existing.id
                self._notify_interested_professional(fresh, returning=True)
            return Response(
                serializers.StatusResponseSerializer({"status": "subscribed"}).data,
                status=status.HTTP_201_CREATED,
            )
        # clicked_for_paid defaults to True on the model for legacy reasons; the
        # interest form no longer collects a pay-to-express-interest choice, so
        # record leads as not-clicked (matches the web /pro_version form).
        interested_pro = serializer.save(clicked_for_paid=False)
        self._notify_interested_professional(interested_pro)
        return Response(
            serializers.StatusResponseSerializer({"status": "subscribed"}).data,
            status=status.HTTP_201_CREATED,
        )

    @staticmethod
    def _notify_interested_professional(
        interested_pro: InterestedProfessional, *, returning: bool = False
    ) -> None:
        """Notify the professional-signup inbox about a REST interest lead.

        Delegates to the shared notify_interested_professional helper so the web
        /pro_version form and this endpoint send an identical inbox format.
        `returning=True` marks a repeat submission from an email that already
        has a lead (the caller passes a transient instance carrying the fresh
        submission with the stored row's pk for the admin link).
        Best-effort: mail failures are logged, not raised, so they never fail
        the already-persisted lead."""
        from fighthealthinsurance.utils import notify_interested_professional

        subject = f"New pro REST signup #{interested_pro.id}"
        if returning:
            subject += " (returning)"
        notify_interested_professional(
            interested_pro,
            source="the Fight Paperwork REST API",
            subject=subject,
        )


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

            from fighthealthinsurance.generate_appeal import is_plausible_identifier

            for i, value in enumerate(extracted_values):
                if i < len(fields):  # Safety check
                    field = fields[i]
                    if not isinstance(value, Exception) and value:
                        # Validate identifier-type fields
                        if (
                            field
                            in (
                                "plan_id",
                                "member_id",
                            )
                            and isinstance(value, str)
                            and not is_plausible_identifier(value)
                        ):
                            logger.debug(f"Rejected implausible {field}: {value}")
                            continue
                        results[field] = value
                    elif isinstance(value, Exception):
                        logger.error(f"Error extracting {field}: {value}")

            return results

        # Run the extraction and get the results
        try:
            extracted_fields = async_to_sync(extract_fields)()

            # Compute confidence notes BEFORE parsing the DOB so the raw
            # string the LLM emitted can be checked against the source text.
            # Always emit a note per expected field (defaulting to "low" when
            # the extractor returned nothing) so callers never have to
            # special-case missing keys.
            from fighthealthinsurance.field_confidence import score_extracted_field

            expected_fields = (
                "patient_name",
                "member_id",
                "dob",
                "plan_id",
                "insurance_company",
            )
            confidence_notes = {
                field: score_extracted_field(field, extracted_fields.get(field), text)
                for field in expected_fields
            }

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

            extracted_fields["confidence_notes"] = confidence_notes

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
                "synthesized": c.synthesized,
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
        # Deduplicate (order-preserving): a candidate was shown once per vote
        # event, and repeated ids would inflate presented counts downstream.
        presented_candidate_ids = list(
            dict.fromkeys(serializer.validated_data["presented_candidate_ids"])
        )

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
