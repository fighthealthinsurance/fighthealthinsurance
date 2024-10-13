import io
import json

from rest_framework.response import Response
from rest_framework.views import APIView

from fighthealthinsurance.common_view_logic import *
from fighthealthinsurance.rest_serializers import *


class Ping(APIView):
    def get(self, request):
        return Response(status=status.HTTP_204_NO_CONTENT)


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
        return AppealsBackendHelper.generate_appeals(pythondata)
