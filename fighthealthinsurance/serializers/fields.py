"""
Custom serializer field types for Fight Health Insurance.

Provides reusable field types for common patterns in the API.
"""
from rest_framework import serializers

from fighthealthinsurance.models import DenialTypes


class StringListField(serializers.ListField):
    """A list field that contains string values."""

    child = serializers.CharField()


class DictionaryListField(serializers.ListField):
    """A list field containing dictionaries with string values."""

    child = serializers.DictField(
        child=serializers.CharField(required=False, allow_blank=True)
    )


class DictionaryStringField(serializers.DictField):
    """A dictionary field with string keys and values."""

    child = serializers.CharField(required=False, allow_blank=True)


class DenialTypesSerializer(serializers.ModelSerializer):
    """Serializer for denial type lookup values."""

    class Meta:
        model = DenialTypes
        fields = ["id", "name"]


class DenialTypesListField(serializers.ListField):
    """A list field containing DenialTypes serializers."""

    child = DenialTypesSerializer()
