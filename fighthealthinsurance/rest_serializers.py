from django import forms

from drf_braces.serializers.form_serializer import (
    FormSerializer,
)
from fighthealthinsurance.core_forms import *
from fighthealthinsurance.models import DenialTypes
from fighthealthinsurance.question_forms import *
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


# Forms
class DeleteDataFormSerializer(FormSerializer):
    class Meta(object):
        form = DeleteDataForm


class ShareAppealFormSerializer(FormSerializer):
    class Meta(object):
        form = ShareAppealForm


class ChooseAppealFormSerializer(FormSerializer):
    class Meta(object):
        form = ChooseAppealForm


class DenialFormSerializer(FormSerializer):
    class Meta(object):
        form = DenialForm
        exclude = ("plan_documents",)


class PostInferedFormSerializer(FormSerializer):
    class Meta(object):
        form = PostInferedForm


class FollowUpFormSerializer(FormSerializer):
    class Meta(object):
        form = FollowUpForm
        exclude = ("followup_documents",)
        field_mapping = {forms.UUIDField: serializers.CharField}
