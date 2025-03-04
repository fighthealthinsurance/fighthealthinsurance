from django import forms
from django.urls import reverse

from drf_braces.serializers.form_serializer import (
    FormSerializer,
)
from fighthealthinsurance import forms as core_forms
from fighthealthinsurance.models import (
    Appeal,
    DenialTypes,
    MailingListSubscriber,
    ProposedAppeal,
    AppealAttachment,
    Denial,
)
from rest_framework import serializers

from fhi_users.auth import rest_serializers as auth_serializers
from typing import Optional, List, Dict, Any
from drf_spectacular.utils import extend_schema_field


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


class ChooseAppealRequestSerializer(serializers.Serializer):
    generated_appeal_text = serializers.CharField()
    editted_appeal_text = serializers.CharField()
    denial_id = serializers.CharField()


class DenialTypesListField(serializers.ListField):
    child = DenialTypesSerializer()


class DenialResponseInfoSerializer(serializers.Serializer):
    selected_denial_type = DenialTypesListField()
    all_denial_types = DenialTypesListField()
    denial_id = serializers.CharField()
    appeal_id = serializers.CharField(required=False)
    your_state = serializers.CharField(required=False)
    procedure = serializers.CharField()
    diagnosis = serializers.CharField()
    semi_sekret = serializers.CharField()
    fax_number = serializers.CharField(required=False)
    date_of_service = serializers.CharField(required=False)
    plan_id = serializers.CharField(required=False)
    claim_id = serializers.CharField(required=False)
    insurance_company = serializers.CharField(required=False)
    date_of_service = serializers.CharField(required=False)


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
    """
    Confirm the details we inferred about the denial.
    """

    class Meta(object):
        form = core_forms.PostInferedForm


class FollowUpFormSerializer(FormSerializer):
    class Meta(object):
        form = core_forms.FollowUpForm
        exclude = ("followup_documents",)
        field_mapping = {forms.UUIDField: serializers.CharField}


class QAResponsesSerializer(serializers.Serializer):
    denial_id = serializers.CharField()
    qa = serializers.DictField(child=serializers.CharField())


# Model serializers


class ProposedAppealSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProposedAppeal


class AppealListRequestSerializer(serializers.Serializer):
    status_filter = serializers.ChoiceField(
        choices=[
            "pending",
            "submitted",
            "overdue",
            "denied",
            "approved",
            "in_progress",
            "all",
        ],
        required=False,
    )
    insurance_company_filter = serializers.CharField(required=False)
    procedure_filter = serializers.CharField(required=False)
    provider_filter = serializers.CharField(required=False)
    patient_filter = serializers.CharField(required=False)
    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)

    page = serializers.IntegerField(min_value=1, required=False, default=1)
    page_size = serializers.IntegerField(min_value=1, required=False, default=10)


class AppealSummarySerializer(serializers.ModelSerializer):
    status = serializers.SerializerMethodField()
    professional_name = serializers.SerializerMethodField()
    patient_name = serializers.SerializerMethodField()
    denial_reason = serializers.SerializerMethodField()
    insurance_company = serializers.SerializerMethodField()

    class Meta:
        model = Appeal
        fields = [
            "id",
            "uuid",
            "status",
            "response_text",
            "response_date",
            "pending",
            "professional_name",
            "patient_name",
            "insurance_company",
            "denial_reason",
            "creation_date",
            "mod_date",
        ]

    @extend_schema_field(serializers.CharField)
    def get_status(self, obj: Appeal) -> str:
        if obj.pending_patient:
            return "pending patient"
        elif obj.pending_professional:
            return "pending professional"
        elif obj.sent:
            return "sent"
        else:
            return "unknown"

    @extend_schema_field(serializers.CharField)
    def get_insurance_company(self, obj: Appeal) -> Optional[str]:
        return obj.for_denial.insurance_company if obj.for_denial else None

    @extend_schema_field(serializers.CharField)
    def get_professional_name(self, obj: Appeal) -> Optional[str]:
        if obj.primary_professional:
            return obj.primary_professional.get_display_name()
        elif obj.creating_professional:
            return obj.creating_professional.get_display_name()
        else:
            return None

    @extend_schema_field(serializers.CharField)
    def get_patient_name(self, obj: Appeal) -> Optional[str]:
        if obj.patient_user:
            return obj.patient_user.get_combined_name()
        return None

    @extend_schema_field(serializers.CharField)
    def get_denial_reason(self, obj: Appeal) -> Optional[str]:
        denial_types: Optional[List[str]] = (
            [x.name for x in obj.for_denial.denial_type.all()]
            if obj.for_denial
            else None
        )
        return ", ".join(denial_types) if denial_types else None


class DenialModelSerializer(serializers.ModelSerializer):
    class Meta:
        model = Denial
        exclude: list[str] = []


class AppealDetailSerializer(serializers.ModelSerializer):
    appeal_pdf_url = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    professional_name = serializers.SerializerMethodField()
    patient_name = serializers.SerializerMethodField()
    denial_reason = serializers.SerializerMethodField()
    insurance_company = serializers.SerializerMethodField()

    class Meta:
        model = Appeal
        fields = [
            "id",
            "uuid",
            "status",
            "response_text",
            "response_date",
            "appeal_text",
            "pending",
            "professional_name",
            "patient_name",
            "insurance_company",
            "denial_reason",
            "creation_date",
            "mod_date",
            "appeal_pdf_url",
        ]

    @extend_schema_field(serializers.CharField)
    def get_status(self, obj: Appeal) -> str:
        if obj.pending_patient:
            return "pending patient"
        elif obj.pending_professional:
            return "pending professional"
        elif obj.sent:
            return "sent"
        else:
            return "unknown"

    @extend_schema_field(serializers.CharField)
    def get_appeal_pdf_url(self, obj: Appeal) -> Optional[str]:
        # Generate a URL for downloading the appeal PDF
        if obj.document_enc:
            # TODO: Use reverse here rather than hardcoding
            return reverse("appeal_file_view", kwargs={"appeal_uuid": obj.uuid})
        return None

    @extend_schema_field(serializers.CharField)
    def get_insurance_company(self, obj: Appeal) -> Optional[str]:
        return obj.for_denial.insurance_company if obj.for_denial else None

    @extend_schema_field(serializers.CharField)
    def get_professional_name(self, obj: Appeal) -> Optional[str]:
        if obj.primary_professional:
            return obj.primary_professional.get_display_name()
        elif obj.creating_professional:
            return obj.creating_professional.get_display_name()
        else:
            return None

    @extend_schema_field(serializers.CharField)
    def get_patient_name(self, obj: Appeal) -> Optional[str]:
        if obj.patient_user:
            return obj.patient_user.get_combined_name()
        return None

    @extend_schema_field(serializers.CharField)
    def get_denial_reason(self, obj: Appeal) -> Optional[str]:
        denial_types: Optional[List[str]] = (
            [x.name for x in obj.for_denial.denial_type.all()]
            if obj.for_denial
            else None
        )
        return ", ".join(denial_types) if denial_types else None


class NotifyPatientRequestSerializer(serializers.Serializer):
    patient_id = serializers.IntegerField()
    include_provider = serializers.BooleanField(default=False)


class AppealFullSerializer(serializers.ModelSerializer):
    appeal_pdf_url = serializers.SerializerMethodField()
    denial = serializers.SerializerMethodField()
    in_userdomain = serializers.SerializerMethodField()
    primary_professional = serializers.SerializerMethodField()

    class Meta:
        model = Appeal
        exclude: list[str] = []

    @extend_schema_field(serializers.CharField)
    def get_appeal_pdf_url(self, obj: Appeal) -> Optional[str]:
        # Generate a URL for downloading the appeal PDF
        if obj.document_enc:
            # TODO: Use reverse here rather than hardcoding
            return reverse("appeal_file_view", kwargs={"appeal_uuid": obj.uuid})
        return None

    def get_denial(self, obj: Appeal) -> Optional[Dict[str, Any]]:
        if obj.for_denial:
            return DenialModelSerializer(obj.for_denial).data  # type: ignore
        return None

    def get_in_userdomain(self, obj: Appeal) -> Optional[Dict[str, Any]]:
        if obj.domain:
            return auth_serializers.UserDomainSerializer(obj.domain).data  # type: ignore
        return None

    def get_primary_professional(self, obj: Appeal) -> Optional[Dict[str, Any]]:
        if obj.primary_professional:
            ser_data: Dict[str, Any] = auth_serializers.FullProfessionalSerializer(
                obj.primary_professional
            ).data  # type: ignore
            return ser_data
        if obj.creating_professional:
            ser_data = auth_serializers.FullProfessionalSerializer(
                obj.creating_professional
            ).data  # type: ignore
            return ser_data
        return None


class AssembleAppealRequestSerializer(serializers.Serializer):
    denial_uuid = serializers.CharField(required=False)
    denial_id = serializers.CharField(required=False)
    completed_appeal_text = serializers.CharField(required=True)
    insurance_company = serializers.CharField(required=False, allow_blank=True)
    fax_phone = serializers.CharField(required=False)
    pubmed_articles_to_include = serializers.ListField(
        child=serializers.CharField(), required=False
    )
    include_provided_health_history = serializers.BooleanField(required=False)


class AssembleAppealResponseSerializer(serializers.Serializer):
    appeal_id = serializers.CharField(required=True)
    status = serializers.CharField(required=False)
    message = serializers.CharField(required=False)


class EmailVerifierSerializer(serializers.Serializer):
    email = serializers.EmailField()
    token = serializers.CharField()
    user_id = serializers.IntegerField()


# Mailing list


class MailingListSubscriberSerializer(serializers.ModelSerializer):
    class Meta:
        model = MailingListSubscriber
        fields = ["email", "name"]


class SendToUserSerializer(serializers.Serializer):
    appeal_id = serializers.IntegerField()
    professional_final_review = serializers.BooleanField()


class SendFax(serializers.Serializer):
    appeal_id = serializers.IntegerField(required=True)
    fax_number = serializers.CharField(required=False)


class InviteProviderSerializer(serializers.Serializer):
    professional_id = serializers.IntegerField(required=False)
    email = serializers.EmailField(required=False)

    def validate(self, data: dict) -> dict:
        if not data.get("professional_id") and not data.get("email"):
            raise serializers.ValidationError(
                "Either professional_id or email must be provided."
            )
        return data


class StatisticsSerializer(serializers.Serializer):
    current_total_appeals = serializers.IntegerField()
    current_success_rate = serializers.FloatField()
    current_total_tips = serializers.IntegerField()
    current_total_patients = serializers.IntegerField()

    previous_total_appeals = serializers.IntegerField()
    previous_success_rate = serializers.FloatField()
    previous_total_tips = serializers.IntegerField()
    previous_total_patients = serializers.IntegerField()

    period_start = serializers.DateTimeField()
    period_end = serializers.DateTimeField()


class SearchResultSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    uuid = serializers.CharField()
    appeal_text = serializers.CharField()
    pending = serializers.BooleanField()
    sent = serializers.BooleanField()
    mod_date = serializers.DateField()
    has_response = serializers.BooleanField()


class AppealAttachmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = AppealAttachment
        fields = ["id", "filename", "mime_type", "created_at"]


class AppealAttachmentUploadSerializer(serializers.Serializer):
    appeal_id = serializers.IntegerField()
    file = serializers.FileField()


class StatusResponseSerializer(serializers.Serializer):
    status = serializers.CharField()
    message = serializers.CharField(required=False, allow_blank=True)


class ErrorSerializer(StatusResponseSerializer):
    error = serializers.CharField()

    def __init__(self, data=None, *args, **kwargs):
        # Set status to "error" if not explicitly provided
        if data and "status" not in data:
            data["status"] = "error"
        # Set message to error value if not explicitly provided
        if data and "error" in data and "message" not in data:
            data["message"] = data["error"]
        super().__init__(data, *args, **kwargs)


class SuccessSerializer(StatusResponseSerializer):
    success = serializers.BooleanField(default=True)

    def __init__(self, data=None, *args, **kwargs):
        # Set status to "success" if not explicitly provided
        if data and "status" not in data:
            data["status"] = "success"
        if data and "message" not in data:
            data["message"] = "Operation completed successfully."
        super().__init__(data, *args, **kwargs)
