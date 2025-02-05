import json
import typing

from asgiref.sync import async_to_sync
from django.conf import settings

from rest_framework import status
from rest_framework import viewsets
from rest_framework.response import Response
from rest_framework.serializers import Serializer
from rest_framework.views import APIView

from fighthealthinsurance import common_view_logic
from fighthealthinsurance.ml.ml_router import ml_router
from fighthealthinsurance import rest_serializers as serializers
from fighthealthinsurance.rest_mixins import (
    SerializerMixin,
    CreateMixin,
    DeleteMixin,
    DeleteOnlyMixin,
)

from stopit import ThreadingTimeout as Timeout


class DataRemovalViewSet(viewsets.ViewSet, DeleteMixin, DeleteOnlyMixin):
    serializer_class = serializers.DeleteDataFormSerializer

    def perform_delete(self, request, serializer):
        email = serializer.validated_data["email"]
        common_view_logic.RemoveDataHelper.remove_data_for_email(email)


class NextStepsViewSet(viewsets.ViewSet, CreateMixin):
    serializer_class = serializers.PostInferedFormSerializer

    def perform_create(self, request, serializer):
        next_step_info = common_view_logic.FindNextStepsHelper.find_next_steps(
            **serializer.validated_data
        )

        return serializers.NextStepInfoSerizableSerializer(
            next_step_info.convert_to_serializable(),
        )


class DenialViewSet(viewsets.ViewSet, CreateMixin):
    serializer_class = serializers.DenialFormSerializer

    def perform_create(self, request, serializer):
        serializer = self.deserialize(data=request.data)
        serializer.is_valid(raise_exception=True)

        denial = common_view_logic.DenialCreatorHelper.create_denial(
            **serializer.validated_data
        )

        return serializers.DenialResponseInfoSerializer(instance=denial)


class FollowUpViewSet(viewsets.ViewSet, CreateMixin):
    serializer_class = serializers.FollowUpFormSerializer

    def perform_create(self, request, serializer):
        common_view_logic.FollowUpHelper.store_follow_up_result(
            **serializer.validated_data
        )

        return None


class Ping(APIView):
    def get(self, request):
        return Response(status=status.HTTP_204_NO_CONTENT)


class CheckStorage(APIView):
    def get(self, request):
        es = settings.EXTERNAL_STORAGE
        with Timeout(2.0):
            es.listdir("./")
            return Response(status=status.HTTP_204_NO_CONTENT)
        return Response(status=status.HTTP_503_SERVICE_UNAVAILABLE)


class CheckMlBackend(APIView):
    def get(self, request):
        if ml_router.working():
            return Response(status=status.HTTP_204_NO_CONTENT)

        return Response(status=status.HTTP_503_SERVICE_UNAVAILABLE)
