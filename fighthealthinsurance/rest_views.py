import typing
from typing import Optional

from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from django.contrib.auth import get_user_model
from django.shortcuts import get_object_or_404
from django.http import FileResponse

from django_encrypted_filefield.crypt import Cryptographer

from rest_framework import status
from rest_framework import viewsets
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.decorators import action


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
from fighthealthinsurance.models import Appeal, Denial, DenialQA, AppealAttachment

from fhi_users.models import (
    UserDomain,
    PatientUser,
    ProfessionalUser,
)

from stopit import ThreadingTimeout as Timeout
from .common_view_logic import AppealAssemblyHelper


if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()

appeal_assembly_helper = AppealAssemblyHelper()


class DataRemovalViewSet(viewsets.ViewSet, DeleteMixin, DeleteOnlyMixin):
    serializer_class = serializers.DeleteDataFormSerializer

    def perform_delete(self, request: Request, serializer):
        email: str = serializer.validated_data["email"]
        common_view_logic.RemoveDataHelper.remove_data_for_email(email)


class NextStepsViewSet(viewsets.ViewSet, CreateMixin):
    serializer_class = serializers.PostInferedFormSerializer

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
        print(self.action)
        if self.action == "create":
            return serializers.DenialFormSerializer
        else:
            return None

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

    def perform_create(self, request: Request, serializer):
        current_user: User = request.user  # type: ignore
        creating_professional = ProfessionalUser.objects.get(user=current_user)
        serializer = self.deserialize(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer_data = serializer.validated_data
        if (
            "primary_professional" in serializer_data
            and serializer_data["primary_professional"] is not None
        ):
            primary_professional = ProfessionalUser.objects.get(
                id=serializer_data.pop("primary_professional")
            )
            serializer_data["primary_professional"] = primary_professional
        denial: Optional[Denial] = None
        if "denial_id" in serializer_data:
            if serializer_data["denial_id"]:
                denial_id = serializer_data.pop("denial_id")
                denial = Denial.filter_to_allowed_denials(current_user).get(
                    denial_id=denial_id
                )
        if "patient_id" in serializer_data:
            patient_id = serializer_data.pop("patient_id")
            if patient_id:
                serializer_data["patient_user"] = PatientUser.objects.get(id=patient_id)
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


class QAResponseViewSet(viewsets.ViewSet, CreateMixin):
    serializer_class = serializers.QAResponsesSerializer

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
        qa_context = ""
        for key, value in DenialQA.objects.filter(denial=denial).values_list(
            "question", "text_answer"
        ):
            qa_context += f"{key}: {value}\n"
        denial.qa_context = qa_context
        denial.save()
        return Response(status=status.HTTP_204_NO_CONTENT)


class FollowUpViewSet(viewsets.ViewSet, CreateMixin):
    serializer_class = serializers.FollowUpFormSerializer

    def perform_create(self, request: Request, serializer):
        common_view_logic.FollowUpHelper.store_follow_up_result(
            **serializer.validated_data
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class Ping(APIView):
    def get(self, request: Request) -> Response:
        return Response(status=status.HTTP_204_NO_CONTENT)


class CheckStorage(APIView):
    def get(self, request: Request) -> Response:
        es = settings.EXTERNAL_STORAGE
        with Timeout(2.0):
            es.listdir("./")
            return Response(status=status.HTTP_204_NO_CONTENT)
        return Response(status=status.HTTP_503_SERVICE_UNAVAILABLE)


class CheckMlBackend(APIView):
    def get(self, request: Request) -> Response:
        if ml_router.working():
            return Response(status=status.HTTP_204_NO_CONTENT)

        return Response(status=status.HTTP_503_SERVICE_UNAVAILABLE)


class AppealViewSet(viewsets.ViewSet, SerializerMixin):
    appeal_assembly_helper = AppealAssemblyHelper()

    def get_serializer_class(self):
        if self.action == "list":
            return serializers.AppaealListRequestSerializer
        elif self.action == "send_fax":
            return serializers.SendFax
        elif self.action == "assemble_appeal":
            return serializers.AssembleAppealRequestSerializer
        elif self.action == "notify_patient":
            return serializers.NotifyPatientRequestSerializer
        else:
            return None

    def list(self, request: Request) -> Response:
        # Lets figure out what appeals they _should_ see
        current_user: User = request.user  # type: ignore
        appeals = Appeal.filter_to_allowed_appeals(current_user)
        # Parse the filters
        input_serializer = self.deserialize(data=request.data)
        # TODO: Handle the filters
        output_serializer = serializers.AppealSummarySerializer(appeals, many=True)
        return Response(output_serializer.data)

    def retrieve(self, request: Request, pk: int) -> Response:
        current_user: User = request.user  # type: ignore
        appeal = get_object_or_404(
            Appeal.filter_to_allowed_appeals(current_user), pk=pk
        )
        serializer = serializers.AppealDetailSerializer(appeal)
        return Response(serializer.data)

    @action(detail=False, methods=["post"])
    def notify_patient(self, request: Request) -> Response:
        serializer = self.deserialize(request.data)
        pk = serializer.validated_data["id"]
        include_professional = serializer.validated_data["professional_name"]
        current_user: User = request.user  # type: ignore
        appeal = get_object_or_404(
            Appeal.filter_to_allowed_appeals(current_user), pk=pk
        )
        patient_user: Optional[PatientUser] = appeal.patient_user
        if patient_user is None:
            return Response(
                {"status": "Patient not found"}, status=status.HTTP_404_NOT_FOUND
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
                    id=request.session["domain_id"]
                ).visible_phone_number,
            )
        else:
            # Notify the patient that there's a free draft appeal to fill in
            common_view_logic.PatientNotificationHelper.notify_of_draft_appeal(
                email=user.email,
                professional_name=professional_name,
                practice_number=UserDomain.objects.get(
                    id=request.session["domain_id"]
                ).visible_phone_number,
            )
        return Response(status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"])
    def get_full_details(self, request: Request, pk: int) -> Response:
        current_user: User = request.user  # type: ignore
        appeal = get_object_or_404(
            Appeal.filter_to_allowed_appeals(current_user), pk=pk
        )
        return Response(serializers.AppealFullSerializer(appeal).data)

    @action(detail=False, methods=["post"])
    def send_fax(self, request) -> Response:
        current_user: User = request.user  # type: ignore
        serializer = self.deserialize(data=request.data)
        serializer.is_valid(raise_exception=True)
        appeal = get_object_or_404(
            Appeal.filter_to_allowed_appeals(current_user), pk=serializer["appeal_id"]
        )
        if serializer["fax_number"] is not None:
            appeal.fax_number = serializer["fax_number"]
            appeal.save()
        patient_user = None
        try:
            patient_user = PatientUser.objects.get(user=current_user)
        except:
            pass
        if (
            appeal.patient_user == patient_user
            and appeal.for_denial.professional_to_finish
        ):
            raise Exception("Provider wants to finish appeal")
        else:
            staged = common_view_logic.SendFaxHelper.stage_appeal_as_fax(
                appeal, email=current_user.email, professional=True
            )
            common_view_logic.SendFaxHelper.remote_send_fax(
                uuid=staged.uuid, hashed_email=staged.hashed_email
            )
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=["post"])
    def assemble_appeal(self, request) -> Response:
        current_user: User = request.user  # type: ignore
        serializer = self.deserialize(data=request.data)
        serializer.is_valid(raise_exception=True)
        # Make sure the user has permission to this denial
        denial_uuid = serializer.validated_data["denial_uuid"]
        denial_opt: Optional[Denial] = None
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
        # Make sure the user has permission to this denial
        denial_uuid = serializer.validated_data["denial_uuid"]
        denial = Denial.filter_to_allowed_denials(current_user).get(
            denial_uuid=denial_uuid
        )
        user_domain = UserDomain.objects.get(id=request.session["domain_id"])
        completed_appeal_text = serializer.validated_data["completed_appeal_text"]
        insurance_company = serializer.validated_data["insurance_company"] or ""
        fax_phone = serializer.validated_data["fax_phone"] or denial.fax_phone
        pubmed_articles_to_include = serializer.validated_data[
            "pubmed_articles_to_include"
        ]
        include_provided_health_history = serializer.validated_data[
            "include_provided_health_history"
        ]
        patient_user = denial.patient_user
        patient_name: str = "unkown"
        if patient_user is not None:
            patient_name = patient_user.get_combined_name()
        appeal = self.appeal_assembly_helper.create_or_update_appeal(
            appeal=appeal,
            name=patient_name,
            insurance_company=insurance_company,
            fax_phone=fax_phone,
            completed_appeal_text=completed_appeal_text,
            pubmed_ids_parsed=pubmed_articles_to_include,
            company_name="Fight Paperwork",
            email=current_user.email,
            include_provided_health_history=include_provided_health_history,
            denial=denial,
            primary_professional=denial.primary_professional,
            creating_professional=denial.creating_professional,
            domain=user_domain,
            cover_template_path="faxes/fpw_cover.html",
            cover_template_string=user_domain.cover_template_string or None,
            company_phone_number="202-938-3266",
            company_fax_number="415-840-7591",
            patient_user=patient_user,
        )
        appeal.save()
        return Response(
            serializers.AssembleAppealResponseSerializer({"appeal_id": appeal.id}),
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["post"])
    def invite_provider(self, request: Request, pk: int) -> Response:
        current_user: User = request.user  # type: ignore
        appeal = get_object_or_404(
            Appeal.filter_to_allowed_appeals(current_user), pk=pk
        )
        serializer = serializers.InviteProviderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
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
                common_view_logic.ProfessionalNotificationHelper.send_signup_invitation(
                    email=email,
                    professional_name=professional.get_display_name(),
                    practice_number=UserDomain.objects.get(
                        id=request.session["domain_id"]
                    ).visible_phone_number,
                )

        return Response({"status": "ok"}, status=status.HTTP_200_OK)


class MailingListSubscriberViewSet(viewsets.ViewSet, CreateMixin, DeleteMixin):
    serializer_class = serializers.MailingListSubscriberSerializer

    def perform_create(self, request: Request, serializer):
        serializer.save()
        return Response({"status": "subscribed"}, status=status.HTTP_201_CREATED)

    def perform_delete(self, request: Request, serializer):
        email = serializer.validated_data["email"]
        MailingListSubscriber.objects.filter(email=email).delete()


class SendToUserViewSet(viewsets.ViewSet, SerializerMixin):
    """Send a draft appeal to a user to fill in."""

    serializer_class = serializers.SendToUserSerializer

    def post(self, request):
        current_user: User = request.user  # type: ignore
        serializer = self.deserialize(request.data)
        serializer.is_valid(raise_exception=True)
        # TODO: Send an e-mail to the patient
        appeal = Appeal.filter_to_allowed_appeals(current_user).get(
            id=serializer.validated_data["appeal_id"]
        )


class AppealAttachmentViewSet(viewsets.ViewSet):
    def list(self, request: Request) -> Response:
        """List attachments for a given appeal"""
        appeal_id = request.query_params.get("appeal_id")
        if not appeal_id:
            return Response(
                {"error": "appeal_id required"}, status=status.HTTP_400_BAD_REQUEST
            )

        current_user: User = request.user  # type: ignore
        appeal = get_object_or_404(
            Appeal.filter_to_allowed_appeals(current_user), id=appeal_id
        )
        attachments = AppealAttachment.objects.filter(appeal=appeal)
        serializer = serializers.AppealAttachmentSerializer(attachments, many=True)
        return Response(serializer.data)

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

    def retrieve(self, request: Request, pk=None) -> FileResponse:
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

    def destroy(self, request: Request, pk=None) -> Response:
        """Delete an attachment"""
        current_user: User = request.user  # type: ignore
        attachment = get_object_or_404(
            AppealAttachment.filter_to_allowed_attachments(current_user), id=pk
        )
        attachment.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
