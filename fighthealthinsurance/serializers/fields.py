"""
Custom serializer field types for Fight Health Insurance.

Provides reusable field types for common patterns in the API.
"""

from rest_framework import serializers

from fighthealthinsurance.models import DenialTypes
from fighthealthinsurance.utils import is_valid_denial_id


class DenialIdField(serializers.CharField):
    """A CharField that validates the value is a positive-integer denial ID."""

    default_error_messages = {"invalid": "Invalid denial_id format"}

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        if not is_valid_denial_id(value):
            self.fail("invalid")
        return value


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
