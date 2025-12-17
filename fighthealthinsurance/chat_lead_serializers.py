from rest_framework import serializers
from fighthealthinsurance.models import ChatLeads


class ChatLeadsSerializer(serializers.ModelSerializer):
    """Serializer for trial chat leads."""

    class Meta:
        model = ChatLeads
        fields = [
            "id",
            "name",
            "email",
            "phone",
            "company",
            "consent_to_contact",
            "agreed_to_terms",
            "session_id",
            "created_at",
            "drug",
            "microsite_slug",
            "referral_source",
            "referral_source_details",
        ]
        read_only_fields = ["id", "session_id", "created_at"]

    def validate(self, data):
        # Ensure required consents are given
        if not data.get("consent_to_contact"):
            raise serializers.ValidationError(
                {"consent_to_contact": "You must consent to being contacted."}
            )
        if not data.get("agreed_to_terms"):
            raise serializers.ValidationError(
                {"agreed_to_terms": "You must agree to the terms of service."}
            )
        return data
