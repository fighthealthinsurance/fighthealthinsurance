from django import forms

from drf_braces.serializers.form_serializer import (
    FormSerializer,
)
from fighthealthinsurance import forms as core_forms
from fighthealthinsurance.models import Appeal, DenialTypes
from rest_framework import serializers


# Common
class StringListField(serializers.ListField):
    child = serializers.CharField()


class DictionaryListField(serializers.ListField):
    child = serializers.DictField(child=serializers.CharField())


# Common View Logic Results
class NextStepInfoSerizableSerializer(serializers.Serializer):
    outside_help_details = StringListField()
    combined_form = DictionaryListField()
    semi_sekret = serializers.CharField()


class DenialTypesSerializer(serializers.ModelSerializer):
    class Meta:
        model = DenialTypes
        fields = ["id", "name"]


class DenialTypesListField(serializers.ListField):
    child = DenialTypesSerializer()


class DenialResponseInfoSerializer(serializers.Serializer):
    selected_denial_type = DenialTypesListField()
    all_denial_types = DenialTypesListField()
    denial_id = serializers.CharField()
    your_state = serializers.CharField()
    procedure = serializers.CharField()
    diagnosis = serializers.CharField()
    semi_sekret = serializers.CharField()


# Signup options


class ProviderSingupSerializer(serializers.Serializer):
    first_name = serializers.CharField()
    middle_name = serializers.CharField()
    last_name = serializers.CharField()
    npi_number = serializers.CharField()
    domain_name = serializers.CharField()


# Forms
class DeleteDataFormSerializer(FormSerializer):
    class Meta(object):
        form = core_forms.DeleteDataForm


class ShareAppealFormSerializer(FormSerializer):
    class Meta(object):
        form = core_forms.ShareAppealForm


class ChooseAppealFormSerializer(FormSerializer):
    class Meta(object):
        form = core_forms.ChooseAppealForm


class DenialFormSerializer(FormSerializer):
    class Meta(object):
        form = core_forms.DenialForm
        exclude = ("plan_documents",)


class PostInferedFormSerializer(FormSerializer):
    class Meta(object):
        form = core_forms.PostInferedForm


class FollowUpFormSerializer(FormSerializer):
    class Meta(object):
        form = core_forms.FollowUpForm
        exclude = ("followup_documents",)
        field_mapping = {forms.UUIDField: serializers.CharField}


class AppealSerializer(serializers.ModelSerializer):
    appeal_pdf_url = serializers.SerializerMethodField()

    class Meta:
        model = Appeal
        fields = [
            "appeal_text",
            "appeal_pdf_url",
        ]

    def get_appeal_pdf_url(self, obj):
        # Generate a URL for downloading the appeal PDF
        if obj.appeal_pdf:
            return f"/appeal-pdf-backend/{obj.id}/download/"
        return None


class AppealRequestSerializer(serializers.Serializer):
    provider_id = serializers.IntegerField()
    patient_id = serializers.IntegerField()
    denial_letter = serializers.FileField(required=False)
    denial_reason = serializers.CharField()
    send_to_patient = serializers.BooleanField(default=False)


class AppealResponseSerializer(serializers.ModelSerializer):
    class Meta:
        model = Appeal
        fields = ["id", "status", "response_text", "response_date"]


class AppealListSerializer(serializers.ModelSerializer):
    class Meta:
        model = Appeal
        fields = ["id", "provider", "patient", "status", "created_at"]


class AppealSubmissionResponseSerializer(serializers.Serializer):
    appeal_id = serializers.IntegerField()
    status = serializers.CharField()
    message = serializers.CharField()
