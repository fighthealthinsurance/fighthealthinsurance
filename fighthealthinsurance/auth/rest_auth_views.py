import json
import typing

from django.conf import settings

from rest_framework import status
from rest_framework import viewsets
from rest_framework.response import Response
from rest_framework.serializers import Serializer
from rest_framework.views import APIView

from fighthealthinsurance.auth import rest_serializers as serializers
