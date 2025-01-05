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

from stopit import ThreadingTimeout as Timeout


class SerializerMixin:
    serializer_class: typing.Optional[typing.Type[Serializer]] = None

    def get_serializer(self, data=None):
        if self.serializer_class is None:
            raise ValueError("serializer_class must be defined and not None")

        return self.serializer_class(data=data)


class CreateMixin(SerializerMixin):
    def perform_create(self, request, serializer):
        pass

    def create(self, request):
        request_serializer = self.get_serializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)
        response_serializer = self.perform_create(request, request_serializer)

        if response_serializer:
            result = response_serializer.data
        else:
            result = None

        return Response(result, status=status.HTTP_201_CREATED)


class DeleteMixin(SerializerMixin):
    def perform_delete(self, request, serializer):
        pass

    def delete(self, request, *args, **kwargs):
        """For some reason"""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_delete(request, serializer, *args, **kwargs)
        return Response(status=status.HTTP_204_NO_CONTENT)


class DeleteOnlyMixin:
    """Extra mixin that allows router display for delete-only resources"""

    def list(self, request, *args, **kwargs):
        # For some reason, delete resources don't show if there's not an
        # associated endpoint for working with their related data. So,
        # this adds one that 404s whenever its used until we can come up
        # with a better solution (or figure out what's wrong)

        return Response(status=status.HTTP_404_NOT_FOUND)


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
        serializer = self.get_serializer(data=request.data)
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
