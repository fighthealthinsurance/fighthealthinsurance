import json
import typing

from asgiref.sync import async_to_sync

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from django.contrib.auth import get_user_model
from django.shortcuts import get_object_or_404
from django.views import View

from rest_framework import status
from rest_framework import viewsets
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import Serializer
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
from fighthealthinsurance.models import Appeal, Denial

from fhi_users.models import (
    UserDomain,
    ProfessionalDomainRelation,
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
        email = serializer.validated_data["email"]
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

    def perform_create(self, request: Request, serializer):
        serializer = self.deserialize(data=request.data)
        serializer.is_valid(raise_exception=True)
        denial = common_view_logic.DenialCreatorHelper.create_denial(
            **serializer.validated_data
        )

        return serializers.DenialResponseInfoSerializer(instance=denial)


class FollowUpViewSet(viewsets.ViewSet, CreateMixin):
    serializer_class = serializers.FollowUpFormSerializer

    def perform_create(self, request: Request, serializer):
        common_view_logic.FollowUpHelper.store_follow_up_result(
            **serializer.validated_data
        )

        return None


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


class AppealViewSet(viewsets.ViewSet):
    appeal_assembly_helper = AppealAssemblyHelper()

    def filter_to_allowed_appeals(self):
        current_user: User = request.user  # type: ignore
        if current_user.is_superuser or current_user.is_staff:
            return Appeal.objects.all()

        query_set = Appeal.objects.none()

        # Patients can view their own appeals
        try:
            patient_user = PatientUser.objects.get(user=current_user)
            query_set |= Appeal.objects.filter(
                patient_user=patient_user,
            )
        except PatientUser.DoesNotExist:
            pass

        # Providers can view appeals they created or were added to as a provider
        # or are a domain admin in.
        try:
            # Appeals they created
            professional_user = ProfessionalUser.objects.get(user=current_user)
            query_set |= Appeal.objects.filter(primary_professional=professional_user)
            # Appeals they were add to.
            additional_appeals = SecondaryAppealProfessionalRelation.objects.filter(
                professional=professional_user
            )
            query_set |= Appeal.objects.filter(
                id__in=[a.appeal.id for a in additional_appeals]
            )
            # Practice/UserDomain admins can view all appeals in their practice
            try:
                user_admin_domains = UserDomain.objects.filter(
                    professionaldomainrelation__professional__user=current_user,
                    professionaldomainrelation__admin=True,
                )
                query_set |= Appeal.objects.filter(domain__in=user_admin_domains)
            except ProfessionalDomainRelation.DoesNotExist:
                pass
        except ProfessionalUser.DoesNotExist:
            pass
        return query_set

    def list(self, request: Request) -> Response:
        # Lets figure out what appeals they _should_ see
        current_user: User = request.user  # type: ignore
        appeals = Appeal.filter_to_allowed_appeals(current_user)
        serializer = serializers.AppealSummarySerializer(appeals, many=True)
        return Response(serializer.data)

    def retrieve(self, request: Request, pk: int) -> Response:
        current_user: User = request.user  # type: ignore
        appeal = get_object_or_404(
            Appeal.filter_to_allowed_appeals(current_user), pk=pk
        )
        serializer = serializers.AppealDetailSerializer(appeal)
        return Response(serializer.data)

    @action(detail=False, methods=["post"])
    def send_fax(self, request) -> Response:
        current_user: User = request.user  # type: ignore
        serializer = serializers.SendFax(request.data)
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
        serializer = serializers.AssembleAppealRequestSerializer(request.data)
        serializer.is_valid(raise_exception=True)
        # Make sure the user has permission to this denial
        denial_uuid = serializer.validated_data["denial_uuid"]
        denial = Denial.filter_to_allowed_denials(current_user).get(
            denial_uuid=denial_uuid
        )
        patient_user = denial.patient_user
        if patient_user is None:
            raise Exception("Patient user not found on denial")
        # Make sure the user has permission to this denial
        denial_uuid = serializer.validated_data["denial_uuid"]
        denial = Denial.filter_to_allowed_denials(current_user).get(
            denial_uuid=denial_uuid
        )
        user_domain = UserDomain.objects.get(request.session["domain_id"])
        completed_appeal_text = serializer.validated_data["completed_appeal_text"]
        insurance_company = serializer.validated_data["insurance_company"] or ""
        fax_phone = serializer.validated_data["fax_phone"] or denial.fax_phone
        pubmed_articles_to_include = serializer.validated_data[
            "pubmed_articles_to_include"
        ]
        include_provided_health_history = serializer.validated_data[
            "include_provided_health_history"
        ]
        appeal = self.appeal_assembly_helper.create_appeal(
            name=denial.patient_user.get_full_name(),
            insurance_company=insurance_company,
            fax_phone=fax_phone,
            completed_appeal_text=completed_appeal_text,
            pubmed_ids_parsed=pubmed_articles_to_include,
            company_name="Fight Paperwork",
            email=current_user.email,
            include_provided_health_history=include_provided_health_history,
            denial=denial,
            professional=denial.primary_professional,
            domain=user_domain,
            cover_template_path="faxes/fpw_cover.html",
            cover_template_string=user_domain.cover_template_string or None,
            company_phone_number="202-938-3266",
            company_fax_number="415-840-7591",
        )
        appeal.primary_professional = denial.primary_professional
        appeal.domain = user_domain
        appeal.patient_user = denial.patient_user
        appeal.save()
        return Response(
            serializers.AssembleAppealResponseSerializer({"appeal_id": appeal.id}),
            status=status.HTTP_201_CREATED,
        )


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
