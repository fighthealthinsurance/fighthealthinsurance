import json
import typing

from asgiref.sync import async_to_sync

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from django.contrib.auth import get_user_model
from django.shortcuts import get_object_or_404

from rest_framework import status
from rest_framework import viewsets
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import Serializer
from rest_framework.views import APIView

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
from fighthealthinsurance.models import Appeal

from fhi_users.models import (
    UserDomain,
    ProfessionalDomainRelation,
    PatientUser,
    ProfessionalUser,
)

from stopit import ThreadingTimeout as Timeout


if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


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
        appeals = self.filter_to_allowed_appeals()
        serializer = serializers.AppealSerializer(appeals, many=True)
        return Response(serializer.data)

    def retrieve(self, request: Request, pk: int) -> Response:
        appeal = get_object_or_404(self.filter_to_allowed_appeals(), pk=pk)
        serializer = serializers.AppealSerializer(appeal)
        return Response(serializer.data)


class MailingListSubscriberViewSet(viewsets.ViewSet, CreateMixin, DeleteMixin):
    serializer_class = serializers.MailingListSubscriberSerializer

    def perform_create(self, request: Request, serializer):
        serializer.save()
        return Response({"status": "subscribed"}, status=status.HTTP_201_CREATED)

    def perform_delete(self, request: Request, serializer):
        email = serializer.validated_data["email"]
        MailingListSubscriber.objects.filter(email=email).delete()
