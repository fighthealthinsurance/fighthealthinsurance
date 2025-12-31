"""
Common serializers for Fight Health Insurance REST API.

Provides status, error, success, and statistics serializers
used across multiple endpoints.
"""

from rest_framework import serializers

from fighthealthinsurance.serializers.fields import (
    StringListField,
    DictionaryListField,
)


class NextStepInfoSerializableSerializer(serializers.Serializer):
    """Serializer for next step information returned after denial analysis."""

    outside_help_details = StringListField()
    combined_form = DictionaryListField()
    semi_sekret = serializers.CharField()


class StatusResponseSerializer(serializers.Serializer):
    """Base serializer for API status responses."""

    status = serializers.CharField()
    message = serializers.CharField(required=False, allow_blank=True)


class LiveModelsStatusSerializer(serializers.Serializer):
    """Serializer for ML model health status endpoint response."""

    alive_models = serializers.IntegerField()
    last_checked = serializers.FloatField(allow_null=True)
    details = serializers.ListField(
        child=serializers.DictField(), required=False, allow_null=True
    )
    message = serializers.CharField(required=False)


class ErrorSerializer(StatusResponseSerializer):
    """Serializer for API error responses with error message."""

    error = serializers.CharField()

    def __init__(self, data=None, *args, **kwargs):
        # Set status to "error" if not explicitly provided
        if data and "status" not in data:
            data["status"] = "error"
        # Set message to error value if not explicitly provided
        if data and "error" in data and "message" not in data:
            data["message"] = data["error"]
        super().__init__(data, *args, **kwargs)


class NotPaidErrorSerializer(ErrorSerializer):
    """Specialized error serializer for payment-required errors."""

    def __init__(self, data=None, *args, **kwargs):
        if data is None:
            data = {}
        data["error"] = "User has not yet paid"
        super().__init__(data, *args, **kwargs)


class SuccessSerializer(StatusResponseSerializer):
    """Serializer for successful API operation responses."""

    success = serializers.BooleanField(default=True)

    def __init__(self, data=None, *args, **kwargs):
        # Set status to "success" if not explicitly provided
        if data and "status" not in data:
            data["status"] = "success"
        if data and "message" not in data:
            data["message"] = "Operation completed successfully."
        super().__init__(data, *args, **kwargs)


class StatisticsSerializer(serializers.Serializer):
    """Serializer for relative appeal statistics comparing two time periods."""

    current_total_appeals = serializers.IntegerField()
    current_pending_appeals = serializers.IntegerField()
    current_sent_appeals = serializers.IntegerField()
    current_success_rate = serializers.FloatField()
    current_estimated_payment_value = serializers.FloatField(
        required=False, allow_null=True
    )
    current_total_patients = serializers.IntegerField()
    previous_total_appeals = serializers.IntegerField()
    previous_pending_appeals = serializers.IntegerField()
    previous_sent_appeals = serializers.IntegerField()
    previous_success_rate = serializers.FloatField()
    previous_estimated_payment_value = serializers.FloatField(
        required=False, allow_null=True
    )
    previous_total_patients = serializers.IntegerField()
    period_start = serializers.DateTimeField()
    period_end = serializers.DateTimeField()


class AbsoluteStatisticsSerializer(serializers.Serializer):
    """Serializer for absolute appeal statistics without time windowing."""

    total_appeals = serializers.IntegerField()
    pending_appeals = serializers.IntegerField()
    sent_appeals = serializers.IntegerField()
    success_rate = serializers.FloatField()
    estimated_payment_value = serializers.FloatField(required=False, allow_null=True)
    total_patients = serializers.IntegerField()


class SearchResultSerializer(serializers.Serializer):
    """Serializer for appeal search result items."""

    id = serializers.IntegerField()
    uuid = serializers.CharField()
    appeal_text = serializers.CharField()
    pending = serializers.BooleanField()
    sent = serializers.BooleanField()
    mod_date = serializers.DateField()
    has_response = serializers.BooleanField()
