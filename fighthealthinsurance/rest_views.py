import typing
from typing import Optional

from django.conf import settings
from django.contrib.auth import get_user_model
from django.shortcuts import get_object_or_404
from django.db.models import Q
from django.utils import timezone
from dateutil.relativedelta import relativedelta
from django.http import FileResponse

from django_encrypted_filefield.crypt import Cryptographer

from rest_framework import status
from rest_framework import viewsets
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.decorators import action

from drf_spectacular.utils import extend_schema
from drf_spectacular.utils import OpenApiParameter
from drf_spectacular.types import OpenApiTypes

from fighthealthinsurance import common_view_logic
from fighthealthinsurance.models import (
    MailingListSubscriber,
    SecondaryAppealProfessionalRelation,
)
from fighthealthinsurance.ml.ml_router import ml_router
from fighthealthinsurance import rest_serializers as serializers
from fighthealthinsurance.rest_mixins import (
    SerializerMixin,
    CreateMixin,
    DeleteMixin,
    DeleteOnlyMixin,
)
from fighthealthinsurance.models import (
    Appeal,
    Denial,
    DenialQA,
    AppealAttachment,
    PubMedMiniArticle,
)
from fighthealthinsurance.pubmed_tools import PubMedTools

from fhi_users.models import (
    UserDomain,
    PatientUser,
    ProfessionalUser,
)

from stopit import ThreadingTimeout as Timeout
from .common_view_logic import AppealAssemblyHelper
from .utils import is_convertible_to_int
import json

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()

appeal_assembly_helper = AppealAssemblyHelper()
pubmed_tools = PubMedTools()


class DataRemovalViewSet(viewsets.ViewSet, DeleteMixin, DeleteOnlyMixin):
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


class NextStepsViewSet(viewsets.ViewSet, CreateMixin):
    serializer_class = serializers.PostInferedFormSerializer

    @extend_schema(responses=serializers.NextStepInfoSerizableSerializer)
    def create(self, request: Request) -> Response:
        return super().create(request)

    @extend_schema(responses=serializers.NextStepInfoSerizableSerializer)
    def perform_create(self, request: Request, serializer):
        next_step_info = common_view_logic.FindNextStepsHelper.find_next_steps(
            **serializer.validated_data
        )

        return serializers.NextStepInfoSerizableSerializer(
            next_step_info.convert_to_serializable(),
        )


class DenialViewSet(viewsets.ViewSet, CreateMixin):
    serializer_class = serializers.DenialFormSerializer

    def get_serializer_class(self):
        if self.action == "create":
            return serializers.DenialFormSerializer
        elif self.action == "get_candidate_articles":
            return serializers.GetCandidateArticlesSerializer
        elif self.action == "select_articles":
            return serializers.SelectArticlesSerializer
        elif self.action == "select_articles_for_fax":
            return serializers.SelectArticlesForFaxSerializer
        else:
            return None

    @extend_schema(responses=serializers.DenialResponseInfoSerializer)
    def create(self, request: Request) -> Response:
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
    def perform_create(self, request: Request, serializer):
        current_user: User = request.user  # type: ignore
        creating_professional = ProfessionalUser.objects.get(user=current_user)
        serializer = self.deserialize(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer_data = serializer.validated_data
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
            if serializer_data["denial_id"] and is_convertible_to_int(
                serializer_data["denial_id"]
            ):
                denial_id = int(serializer_data.pop("denial_id"))
                denial = Denial.filter_to_allowed_denials(current_user).get(
                    denial_id=denial_id
                )
            else:
                del serializer_data["denial_id"]
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
        denial_response_info = (
            common_view_logic.DenialCreatorHelper.create_or_update_denial(
                denial=denial,
                creating_professional=creating_professional,
                **serializer_data,
            )
        )
        denial = Denial.objects.get(uuid=denial_response_info.uuid)
        # Creating a pending appeal
        try:
            Appeal.objects.get(for_denial=denial)
        except:
            appeal = Appeal.objects.create(
                for_denial=denial,
                patient_user=denial.patient_user,
                primary_professional=denial.primary_professional,
                creating_professional=denial.creating_professional,
                pending=True,
            )
            denial_response_info.appeal_id = appeal.id
        return serializers.DenialResponseInfoSerializer(instance=denial_response_info)

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

        max_results = serializer.validated_data.get("max_results", 5)

        # Get user-provided PubMed IDs if present
        raw_pmids = serializer.validated_data.get("raw_pmids", [])

        # Check for domain-specific default PubMed IDs
        domain_pmids = []
        if denial.domain and denial.domain.default_pubmed_ids:
            try:
                domain_pmids = json.loads(denial.domain.default_pubmed_ids)
            except (json.JSONDecodeError, AttributeError):
                pass

        # Find candidate articles
        articles = pubmed_tools.find_candidate_articles_for_denial(
            denial, max_results=max_results, additional_pmids=(raw_pmids + domain_pmids)
        )

        return Response(
            serializers.PubMedMiniArticleSerializer(articles, many=True).data,
            status=status.HTTP_200_OK,
        )

    @extend_schema(responses=serializers.StatusResponseSerializer)
    @action(detail=False, methods=["post"])
    def select_articles(self, request: Request) -> Response:
        """Select PubMed articles to include in the denial and appeal context."""
        serializer = self.deserialize(data=request.data)
        serializer.is_valid(raise_exception=True)

        current_user: User = request.user  # type: ignore
        denial = get_object_or_404(
            Denial.filter_to_allowed_denials(current_user),
            denial_id=serializer.validated_data["denial_id"],
        )

        pmids = serializer.validated_data["pmids"]
        denial.pubmed_ids_json = json.dumps(pmids)
        denial.save()

        # If there's a pending appeal for this denial, update it too
        try:
            appeal = Appeal.filter_to_allowed_appeals(current_user).get(
                for_denial=denial, pending=True
            )
            if appeal:
                appeal.pubmed_ids_json = json.dumps(pmids)
                appeal.save()
        except Appeal.DoesNotExist:
            pass

        return Response(
            serializers.SuccessSerializer(
                {"message": f"Selected {len(pmids)} articles for this denial"}
            ).data,
            status=status.HTTP_200_OK,
        )

    @extend_schema(responses=serializers.StatusResponseSerializer)
    @action(detail=False, methods=["post"])
    def select_articles_for_fax(self, request: Request) -> Response:
        """Select PubMed articles to include in the fax for an appeal."""
        serializer = self.deserialize(data=request.data)
        serializer.is_valid(raise_exception=True)
        current_user: User = request.user  # type: ignore

        # Get the denial
        denial = get_object_or_404(
            Denial.filter_to_allowed_denials(current_user),
            denial_id=serializer.validated_data["denial_id"],
        )

        # Get the appeal ID if provided, otherwise find a pending appeal
        appeal_id = serializer.validated_data.get("appeal_id", None)
        if appeal_id:
            appeal = get_object_or_404(
                Appeal.filter_to_allowed_appeals(current_user),
                id=appeal_id,
            )
        else:
            try:
                appeal = Appeal.filter_to_allowed_appeals(current_user).get(
                    for_denial=denial, pending=True
                )
            except Appeal.DoesNotExist:
                # Create a pending appeal if none exists
                appeal = Appeal.objects.create(
                    for_denial=denial,
                    patient_user=denial.patient_user,
                    primary_professional=denial.primary_professional,
                    creating_professional=denial.creating_professional,
                    pending=True,
                )

        # Set the selected PMIDs for fax inclusion
        pmids = serializer.validated_data["pmids"]
        appeal.pubmed_ids_json = json.dumps(pmids)
        appeal.save()

        return Response(
            serializers.SuccessSerializer(
                {"message": f"Selected {len(pmids)} articles to include in the fax"}
            ).data,
            status=status.HTTP_200_OK,
        )


class QAResponseViewSet(viewsets.ViewSet, CreateMixin):
    serializer_class = serializers.QAResponsesSerializer

    @extend_schema(responses=serializers.StatusResponseSerializer)
    def create(self, request: Request) -> Response:
        return super().create(request)

    @extend_schema(responses=serializers.StatusResponseSerializer)
    def perform_create(self, request: Request, serializer):
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
    serializer_class = serializers.FollowUpFormSerializer

    @extend_schema(responses=serializers.StatusResponseSerializer)
    def create(self, request: Request) -> Response:
        return super().create(request)

    @extend_schema(responses=serializers.StatusResponseSerializer)
    def perform_create(self, request: Request, serializer):
        common_view_logic.FollowUpHelper.store_follow_up_result(
            **serializer.validated_data
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class Ping(APIView):
    @extend_schema(responses=serializers.StatusResponseSerializer)
    def get(self, request: Request) -> Response:
        return Response(status=status.HTTP_204_NO_CONTENT)


class CheckStorage(APIView):
    @extend_schema(responses=serializers.StatusResponseSerializer)
    def get(self, request: Request) -> Response:
        es = settings.EXTERNAL_STORAGE
        with Timeout(2.0):
            es.listdir("./")
            return Response(status=status.HTTP_204_NO_CONTENT)
        return Response(status=status.HTTP_503_SERVICE_UNAVAILABLE)


class CheckMlBackend(APIView):
    @extend_schema(responses=serializers.StatusResponseSerializer)
    def get(self, request: Request) -> Response:
        if ml_router.working():
            return Response(status=status.HTTP_204_NO_CONTENT)

        return Response(status=status.HTTP_503_SERVICE_UNAVAILABLE)


class AppealViewSet(viewsets.ViewSet):
    """
    API endpoint for managing appeals.
    """

    serializer_class = serializers.AppealSerializer

    def get_serializer_class(self):
        if self.action == "assemble_appeal":
            return serializers.AssembleAppealSerializer
        elif self.action == "get_full_details":
            return serializers.AppealSerializer
        elif self.action == "notify_patient":
            return serializers.NotifyPatientSerializer
        elif self.action == "send_fax":
            return serializers.SendFaxSerializer
        elif self.action == "invite_provider":
            return serializers.InviteProviderSerializer
        elif self.action == "select_articles_for_fax":
            return serializers.SelectArticlesForFaxSerializer
        else:
            return serializers.AppealSerializer

    def get_queryset(self):
        current_user: User = self.request.user  # type: ignore
        return models.Appeal.filter_to_allowed_appeals(current_user)

    @action(detail=False, methods=["get"])
    def get_full_details(self, request):
        """
        Get full details of an appeal, including denial and related entities.
        """
        pk = request.query_params.get("pk")
        if not pk:
            return Response(
                {"error": "Appeal ID is required"}, status=status.HTTP_400_BAD_REQUEST
            )

        current_user: User = request.user  # type: ignore
        appeal = get_object_or_404(
            models.Appeal.filter_to_allowed_appeals(current_user), id=pk
        )
        serializer = serializers.AppealSerializer(appeal)
        return Response(serializer.data)

    @action(detail=False, methods=["post"])
    def assemble_appeal(self, request):
        """
        Create or update an appeal for a denial.
        """
        # ...existing code...

    @action(detail=False, methods=["post"])
    def notify_patient(self, request):
        """
        Send a notification to the patient about their appeal.
        """
        # ...existing code...

    @action(detail=False, methods=["post"])
    def send_fax(self, request):
        """
        Send an appeal via fax.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        current_user: User = request.user  # type: ignore
        appeal_id = serializer.validated_data.get("appeal_id")
        fax_number = serializer.validated_data.get("fax_number")

        appeal = get_object_or_404(
            models.Appeal.filter_to_allowed_appeals(current_user), id=appeal_id
        )

        # Check permissions based on user role
        is_professional = False
        try:
            professional = models.ProfessionalUser.objects.get(user=current_user)
            is_professional = True
        except models.ProfessionalUser.DoesNotExist:
            pass

        is_patient = False
        try:
            patient = models.PatientUser.objects.get(user=current_user)
            is_patient = True
        except models.PatientUser.DoesNotExist:
            pass

        # Professional can always send
        if is_professional:
            # Update appeal status
            appeal.pending = False
            appeal.pending_patient = False
            appeal.pending_professional = False
            appeal.save()

            # Create fax record
            # Include PubMed articles if they were selected
            pubmed_ids = []
            if appeal.pubmed_ids_json:
                try:
                    pubmed_ids = json.loads(appeal.pubmed_ids_json)
                except json.JSONDecodeError:
                    pass

            fax = models.FaxesToSend.objects.create(
                email=appeal.patient_user.user.email if appeal.patient_user else "",
                appeal_text=appeal.appeal_text,
                hashed_email=(
                    models.Denial.get_hashed_email(appeal.patient_user.user.email)
                    if appeal.patient_user
                    else ""
                ),
                name=(
                    appeal.patient_user.get_legal_name() if appeal.patient_user else ""
                ),
                paid=True,
                for_appeal=appeal,
                destination=fax_number,
                should_send=True,
                professional=True,
                pmids=",".join(pubmed_ids) if pubmed_ids else "",
            )
            appeal.fax = fax
            appeal.sent = True
            appeal.save()

            return Response(status=status.HTTP_204_NO_CONTENT)

        # Patient sending - check if needs professional approval
        elif is_patient:
            # Check if the denial requires professional to finish
            if appeal.for_denial.professional_to_finish:
                # Mark as pending for professional
                appeal.pending = True
                appeal.pending_patient = False
                appeal.pending_professional = True
                appeal.save()

                return Response(
                    {"message": "Pending professional approval before sending"},
                    status=status.HTTP_200_OK,
                )
            else:
                # Patient can send directly
                appeal.pending = False
                appeal.pending_patient = False
                appeal.pending_professional = False
                appeal.save()

                # Create fax record
                # Include PubMed articles if they were selected
                pubmed_ids = []
                if appeal.pubmed_ids_json:
                    try:
                        pubmed_ids = json.loads(appeal.pubmed_ids_json)
                    except json.JSONDecodeError:
                        pass

                fax = models.FaxesToSend.objects.create(
                    email=appeal.patient_user.user.email if appeal.patient_user else "",
                    appeal_text=appeal.appeal_text,
                    hashed_email=(
                        models.Denial.get_hashed_email(appeal.patient_user.user.email)
                        if appeal.patient_user
                        else ""
                    ),
                    name=(
                        appeal.patient_user.get_legal_name()
                        if appeal.patient_user
                        else ""
                    ),
                    paid=True,
                    for_appeal=appeal,
                    destination=fax_number,
                    should_send=True,
                    professional=False,
                    pmids=",".join(pubmed_ids) if pubmed_ids else "",
                )
                appeal.fax = fax
                appeal.sent = True
                appeal.save()

                return Response(status=status.HTTP_204_NO_CONTENT)

        return Response(
            {"error": "Permission denied"}, status=status.HTTP_403_FORBIDDEN
        )

    @action(detail=False, methods=["post"])
    def invite_provider(self, request):
        """
        Invite a provider to collaborate on an appeal.
        """
        # ...existing code...

    @action(detail=False, methods=["get"])
    def stats(self, request):
        """
        Get statistics about appeals.
        """
        # ...existing code...

    @action(detail=False, methods=["get"])
    def absolute_stats(self, request):
        """
        Get absolute statistics (not relative to any time period).
        """
        # ...existing code...

    @action(detail=False, methods=["post"])
    def select_articles_for_fax(self, request):
        """
        Select PubMed articles to include in the fax for an appeal.
        """
        serializer = serializers.SelectArticlesForFaxSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        current_user: User = request.user  # type: ignore

        # Get the denial
        denial = get_object_or_404(
            models.Denial.filter_to_allowed_denials(current_user),
            denial_id=serializer.validated_data["denial_id"],
        )

        # Get the appeal ID if provided, otherwise find a pending appeal
        appeal_id = serializer.validated_data.get("appeal_id", None)
        if appeal_id:
            appeal = get_object_or_404(
                models.Appeal.filter_to_allowed_appeals(current_user),
                id=appeal_id,
            )
        else:
            try:
                appeal = models.Appeal.filter_to_allowed_appeals(current_user).get(
                    for_denial=denial, pending=True
                )
            except models.Appeal.DoesNotExist:
                # Create a pending appeal if none exists
                appeal = models.Appeal.objects.create(
                    for_denial=denial,
                    patient_user=denial.patient_user,
                    primary_professional=denial.primary_professional,
                    creating_professional=denial.creating_professional,
                    pending=True,
                    domain=denial.domain,
                    hashed_email=denial.hashed_email,
                )

        # Set the selected PMIDs for fax inclusion
        pmids = serializer.validated_data["pmids"]
        appeal.pubmed_ids_json = json.dumps(pmids)
        appeal.save()

        return Response(
            {"message": f"Selected {len(pmids)} articles to include in the fax"},
            status=status.HTTP_200_OK,
        )


class MailingListSubscriberViewSet(viewsets.ViewSet, CreateMixin, DeleteMixin):
    serializer_class = serializers.MailingListSubscriberSerializer

    @extend_schema(responses=serializers.StatusResponseSerializer)
    def create(self, request: Request) -> Response:
        return super().create(request)

    @extend_schema(responses=serializers.StatusResponseSerializer)
    def perform_create(self, request: Request, serializer):
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

        current_user: User = request.user  # type: ignore
        appeal = get_object_or_404(
            Appeal.filter_to_allowed_appeals(current_user), id=appeal_id
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

        attachment = AppealAttachment.objects.create(
            appeal=appeal, file=file, filename=file.name, mime_type=file.content_type
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
