import json
import typing

from django.conf import settings

from rest_framework import status
from rest_framework import viewsets
from rest_framework.response import Response
from rest_framework.serializers import Serializer
from rest_framework.views import APIView

from fhi_users.models import UserDomain, ProfessionalUser
from fhi_users.auth import rest_serializers as serializers
from fighthealthinsurance.rest_mixins import (
    SerializerMixin,
    CreateMixin,
    DeleteMixin,
    DeleteOnlyMixin,
)


class CreateProfessionalUser(viewsets.ViewSet, CreateMixin):
    serializer_class = serializers.ProfessionalSignupSerializer

    def perform_create(self, request, serializer):
        data = serializer.validated_data
        if not data["make_new_domain"]:
            domain = UserDomain.objects.filter(name=data["domain_name"]).get()
        return None
