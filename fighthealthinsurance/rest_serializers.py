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
)
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
    your_state = serializers.CharField()
    procedure = serializers.CharField()
    diagnosis = serializers.CharField()
    semi_sekret = serializers.CharField()


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
    primary_professional = serializers.IntegerField(required=False)
    user_id = forms.IntegerField(required=True)

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


# Model serializers


class ProposedAppealSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProposedAppeal


class AppaealListRequestSerializer(serializers.Serializer):
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


class AppealSummarySerializer(serializers.ModelSerializer):
    status = serializers.SerializerMethodField()

    class Meta:
        model = Appeal
        fields = ["uuid", "status", "response_text", "response_date" "pending"]

    def get_status(self, obj):
        if obj.pending_patient:
            return "pending patient"
        elif obj.pending_professional:
            return "pending professional"
        elif obj.sent:
            return "sent"
        else:
            return "unkown"


class AppealDetailSerializer(serializers.ModelSerializer):
    appeal_pdf_url = serializers.SerializerMethodField()

    class Meta:
        model = Appeal
        fields = [
            "uuid",
            "status",
            "response_text",
            "response_date",
            "appeal_text",
            "appeal_pdf_url",
            "pending",
        ]

    def get_appeal_pdf_url(self, obj):
        # Generate a URL for downloading the appeal PDF
        if obj.appeal_pdf:
            # TODO: Use reverse here rather than hardcoding
            return reverse("appeal_file_view", kwargs={"appeal_uuid": obj.uuid})
        return None


class NotifyPatientRequestSerializer(serializers.Serializer):
    patient_id = serializers.IntegerField()
    include_provider = serializers.BooleanField(default=False)


class AppealFullSerializer(serializers.ModelSerializer):

    class Meta:
        model = Appeal
        exclude: list[str] = []


class AssembleAppealRequestSerializer(serializers.Serializer):
    denial_uuid = serializers.CharField(required=True)
    completed_appeal_text = serializers.CharField(required=True)
    insurance_company = serializers.CharField(required=False)
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

    def validate(self, data):
        if not data.get("professional_id") and not data.get("email"):
            raise serializers.ValidationError(
                "Either professional_id or email must be provided."
            )
        return data
