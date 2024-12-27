from asgiref.sync import async_to_sync
import json

from fighthealthinsurance.common_view_logic import *
from fighthealthinsurance.rest_serializers import *
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import status

from stopit import ThreadingTimeout as Timeout


class Ping(APIView):
    def get(self, request):
        return Response(status=status.HTTP_204_NO_CONTENT)


class CheckStorage(APIView):
    def get(self, request):
        es = settings.EXTERNAL_STORAGE
        with Timeout(2.0) as _timeout_ctx:
            list = es.listdir("./")
            return Response(status=status.HTTP_204_NO_CONTENT)
        return Response(status=status.HTTP_503_SERVICE_UNAVAILABLE)


class CheckMlBackend(APIView):
    def get(self, request):
        from fighthealthinsurance.model_router import model_router

        if model_router.working():
            return Response(status=status.HTTP_204_NO_CONTENT)
        else:
            return Response(status=status.HTTP_503_SERVICE_UNAVAILABLE)


class RemoveData(APIView):
    def delete(self, request):
        pythondata = json.loads(request.body)
        # Make sure we got what we expected
        serializer = DeleteDataFormSerializer(data=pythondata)
        if serializer.is_valid():
            email = serializer.validated_data["email"]
            RemoveDataHelper.remove_data_for_email(email)
            return Response(status=status.HTTP_204_NO_CONTENT)
        else:
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class FindNextSteps(APIView):
    def post(self, request):
        pythondata = json.loads(request.body)
        # Make sure we got what we expected
        serializer = PostInferedFormSerializer(data=pythondata)
        if serializer.is_valid():
            print(f"Got {serializer}")
            next_step_info = FindNextStepsHelper.find_next_steps(
                **serializer.validated_data
            )
            return Response(
                NextStepInfoSerizableSerializer(
                    next_step_info.convert_to_serializable()
                ).data
            )
        else:
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class DenialCreator(APIView):
    def post(self, request):
        pythondata = json.loads(request.body)
        # Make sure we got what we expected
        serializer = DenialFormSerializer(data=pythondata)
        if serializer.is_valid():
            denial_response = DenialCreatorHelper.create_denial(
                **serializer.validated_data
            )
            return Response(DenialResponseInfoSerializer(denial_response).data)
        else:
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class FollowUpAPI(APIView):
    def post(self, request):
        pythondata = json.loads(request.body)
        # Make sure we got what we expected
        serializer = FollowUpFormSerializer(data=pythondata)
        if serializer.is_valid():
            response = FollowUpHelper.store_follow_up_result(
                **serializer.validated_data
            )
            return Response(status=status.HTTP_204_NO_CONTENT)
        else:
            print(f"Serialization error {serializer.errors}")
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class AppealsBackend(APIView):
    """Streaming back the appeals as json :D"""

    def post(self, request):
        pythondata = json.loads(request.body)
        denial_id = pythondata["denial_id"]
        return async_to_sync(AppealsBackendHelper.generate_appeals)(pythondata)


class StreamingEntityBackend(APIView):
    """Streaming back the updates as json :D"""

    def post(self, request):
        pythondata = json.loads(request.body)
        denial_id: int = pythondata["denial_id"]
        return async_to_sync(DenialCreatorHelper.extract_entity)(denial_id)
