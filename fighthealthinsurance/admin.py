from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from loguru import logger

from fhi_users.audit import AuditLog
from fhi_users.models import (
    PatientUser,
    ProfessionalDomainRelation,
    ProfessionalUser,
    UserDomain,
)
from fighthealthinsurance.models import (
    Appeal,
    AppealAttachment,
    AppealTemplates,
    ChatLeads,
    ChooserCandidate,
    ChooserTask,
    ChooserVote,
    DataSource,
    Denial,
    DenialQA,
    DenialTypes,
    DenialTypesRelation,
    Diagnosis,
    FaxesToSend,
    FollowUp,
    FollowUpDocuments,
    FollowUpSched,
    FollowUpType,
    FuzzAttempt,
    GenericContextGeneration,
    GenericQuestionGeneration,
    InsuranceCompany,
    InsurancePlan,
    InterestedProfessional,
    MailingListSubscriber,
    OngoingChat,
    PlanDocuments,
    PlanSource,
    PlanSourceRelation,
    PlanType,
    PlanTypesRelation,
    PriorAuthRequest,
    Procedures,
    ProposedAppeal,
    ProposedPriorAuth,
    PubMedArticleSummarized,
    PubMedQueryData,
    Regulator,
    SecondaryAppealProfessionalRelation,
    SecondaryDenialProfessionalRelation,
    StripePrice,
    StripeProduct,
)


@admin.register(ChatLeads)
class ChatLeadsAdmin(admin.ModelAdmin):
    """Chat Leads"""

    list_display = (
        "name",
        "company",
        "email",
        "referral_source",
    )
    search_fields = ("company", "name", "referral_source", "referral_source_details")
    list_filter = ("referral_source",)
    ordering = ("-created_at",)
    fields = (
        "name",
        "email",
        "phone",
        "company",
        "drug",
        "microsite_slug",
        "referral_source",
        "referral_source_details",
        "consent_to_contact",
        "agreed_to_terms",
        "session_id",
        "created_at",
    )
    readonly_fields = ("session_id", "created_at")


@admin.register(PriorAuthRequest)
class PriorAuthRequestAdmin(admin.ModelAdmin):
    """Prior Authorization Request"""

    list_display = (
        "created_at",
        "id",
        "diagnosis",
        "treatment",
        "insurance_company",
        "patient_name",
        "chat",
    )
    search_fields = ("diagnosis", "treatment", "insurance_company")
    ordering = ("-created_at",)


@admin.register(ProposedPriorAuth)
class ProposedPriorAuthAdmin(admin.ModelAdmin):
    """Proposed Prior Authorization Request"""

    list_display = ("prior_auth_request_id", "proposed_id")
    search_fields = ()
    ordering = ("-proposed_id",)


@admin.register(GenericContextGeneration)
class GenericContextGenerationAdmin(admin.ModelAdmin):
    """Generic Context"""


@admin.register(GenericQuestionGeneration)
class GenericQuestionsGenerationAdmin(admin.ModelAdmin):
    """Generic Questions Context"""


@admin.register(UserDomain)
class UserDomainAdmin(admin.ModelAdmin):
    """User domains"""

    list_display = ("id", "name", "visible_phone_number")


@admin.register(ProfessionalUser)
class ProfessionalUserAdmin(admin.ModelAdmin):
    """User domains"""

    list_display = ("id", "user", "user__first_name", "user__email")


@admin.register(ProfessionalDomainRelation)
class ProfessionalDomainRelationAdmin(admin.ModelAdmin):
    """ProfessionalDomainRelation domains"""

    list_display = (
        "professional",
        "admin",
        "domain",
        "professional__user",
        "professional__user__first_name",
        "professional__user__email",
    )


@admin.register(PatientUser)
class PatientUserAdmin(admin.ModelAdmin):
    """User domains"""

    list_display = ("id", "user", "user__first_name", "user__email")


@admin.register(Denial)
class DenialAdmin(admin.ModelAdmin):
    """Admin configuration for Denial model."""

    list_display = (
        "denial_id",
        "date",
        "raw_email",
        "insurance_company_obj",
        "insurance_plan_obj",
        "patient_visible",
        "appeal_result",
        "referral_source",
    )
    search_fields = (
        "raw_email",
        "denial_text",
        "insurance_company",
        "insurance_company_obj__name",
        "insurance_plan_obj__plan_name",
        "referral_source",
        "referral_source_details",
    )
    list_filter = (
        ("raw_email", admin.EmptyFieldListFilter),
        "insurance_company_obj",
        "insurance_plan_obj",
        "plan_source__name",
        "plan_type__name",
        "denial_type__name",
        "date",
        ("appeal_text", admin.EmptyFieldListFilter),
        "referral_source",
    )
    ordering = ("-date",)
    autocomplete_fields = ["insurance_company_obj", "insurance_plan_obj"]


@admin.register(InterestedProfessional)
class InterestedProfessionalAdmin(admin.ModelAdmin):
    """Admin configuration for InterestedProfessional model."""

    list_display = (
        "id",
        "name",
        "email",
        "paid",
        "signup_date",
    )
    search_fields = ("name", "email", "business_name")
    list_filter = ("paid", "signup_date", "mod_date")
    ordering = ("-signup_date",)


@admin.register(MailingListSubscriber)
class MailingListSubscriberAdmin(admin.ModelAdmin):
    """Admin configuration for MailingListSubscriber model."""

    list_display = ("id", "email", "name", "signup_date", "referral_source")
    search_fields = ("email", "name", "referral_source", "referral_source_details")
    list_filter = ("signup_date", "referral_source")
    ordering = ("-signup_date",)
    fields = (
        "email",
        "name",
        "phone",
        "referral_source",
        "referral_source_details",
        "comments",
        "signup_date",
        "unsubscribe_token",
    )
    readonly_fields = ("signup_date", "unsubscribe_token")


@admin.register(FollowUpType)
class FollowUpTypeAdmin(admin.ModelAdmin):
    """Admin configuration for FollowUpType model."""

    list_display = ("id", "name", "subject", "duration")
    search_fields = ("name", "subject", "text")
    ordering = ("name",)


@admin.register(FollowUp)
class FollowUpAdmin(admin.ModelAdmin):
    """Admin configuration for FollowUp model."""

    list_display = (
        "followup_result_id",
        "email",
        "denial_id",
        "response_date",
        "more_follow_up_requested",
    )
    search_fields = ("email", "appeal_result", "user_comments")
    list_filter = ("response_date", "more_follow_up_requested")
    ordering = ("-response_date",)


@admin.register(FollowUpSched)
class FollowUpSchedAdmin(admin.ModelAdmin):
    """Admin configuration for FollowUpSched model."""

    list_display = (
        "follow_up_id",
        "email",
        "follow_up_date",
        "follow_up_sent",
        "denial_id",
    )
    search_fields = ("email",)
    list_filter = ("follow_up_date", "follow_up_sent")
    ordering = ("-follow_up_date",)


@admin.register(PlanType)
class PlanTypeAdmin(admin.ModelAdmin):
    """Admin configuration for PlanType model."""

    list_display = ("id", "name", "alt_name")
    search_fields = ("name", "alt_name")
    ordering = ("name",)


@admin.register(Regulator)
class RegulatorAdmin(admin.ModelAdmin):
    """Admin configuration for Regulator model."""

    list_display = ("id", "name", "website", "alt_name")
    search_fields = ("name", "website")
    ordering = ("name",)


@admin.register(PlanSource)
class PlanSourceAdmin(admin.ModelAdmin):
    """Admin configuration for PlanSource model."""

    list_display = ("id", "name")
    search_fields = ("name",)
    ordering = ("name",)


@admin.register(InsuranceCompany)
class InsuranceCompanyAdmin(admin.ModelAdmin):
    """Admin configuration for InsuranceCompany model."""

    list_display = ("id", "name", "website", "is_tpa", "is_marketplace_focused")
    list_filter = ("is_tpa", "is_marketplace_focused")
    search_fields = ("name", "alt_names")
    ordering = ("name",)
    fieldsets = (
        (
            None,
            {
                "fields": ("name", "alt_names", "website", "notes"),
            },
        ),
        (
            "Company Type",
            {
                "fields": ("is_tpa", "is_marketplace_focused"),
                "description": "Flags to indicate company type for suggestions",
            },
        ),
        (
            "Pattern Matching",
            {
                "fields": ("regex", "negative_regex"),
                "classes": ("collapse",),
            },
        ),
    )


@admin.register(InsurancePlan)
class InsurancePlanAdmin(admin.ModelAdmin):
    """Admin configuration for InsurancePlan model."""

    list_display = (
        "id",
        "insurance_company",
        "plan_name",
        "state",
        "plan_type",
        "plan_source",
    )
    list_filter = ("insurance_company", "state", "plan_type", "plan_source")
    search_fields = ("plan_name", "insurance_company__name", "notes")
    ordering = ("insurance_company__name", "plan_name")
    autocomplete_fields = ["insurance_company"]
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "insurance_company",
                    "plan_name",
                    "state",
                    "plan_type",
                    "plan_source",
                    "plan_id_prefix",
                    "notes",
                ),
            },
        ),
        (
            "Pattern Matching",
            {
                "fields": ("regex", "negative_regex"),
                "classes": ("collapse",),
            },
        ),
    )


@admin.register(Diagnosis)
class DiagnosisAdmin(admin.ModelAdmin):
    """Admin configuration for Diagnosis model."""

    list_display = ("id", "name")
    search_fields = ("name",)
    ordering = ("id",)


@admin.register(Procedures)
class ProceduresAdmin(admin.ModelAdmin):
    """Admin configuration for Procedures model."""

    list_display = ("id", "name")
    search_fields = ("name",)
    ordering = ("id",)


@admin.register(DenialTypes)
class DenialTypesAdmin(admin.ModelAdmin):
    """Admin configuration for DenialTypes model."""

    list_display = ("id", "name", "parent")
    search_fields = ("name",)
    list_filter = ("parent",)
    ordering = ("name",)


@admin.register(AppealTemplates)
class AppealTemplatesAdmin(admin.ModelAdmin):
    """Admin configuration for AppealTemplates model."""

    list_display = ("id", "name")
    search_fields = ("name", "appeal_text")
    ordering = ("name",)


@admin.register(DataSource)
class DataSourceAdmin(admin.ModelAdmin):
    """Admin configuration for DataSource model."""

    list_display = ("id", "name")
    search_fields = ("name",)
    ordering = ("id",)


@admin.register(PlanDocuments)
class PlanDocumentsAdmin(admin.ModelAdmin):
    """Admin configuration for PlanDocuments model."""

    list_display = ("plan_document_id", "denial")
    search_fields = ("denial__denial_text",)
    ordering = ("-plan_document_id",)


@admin.register(FollowUpDocuments)
class FollowUpDocumentsAdmin(admin.ModelAdmin):
    """Admin configuration for FollowUpDocuments model."""

    list_display = ("document_id", "denial", "follow_up_id")
    search_fields = ("denial__denial_text",)
    ordering = ("-document_id",)


@admin.register(PubMedArticleSummarized)
class PubMedArticleSummarizedAdmin(admin.ModelAdmin):
    """Admin configuration for PubMedArticleSummarized model."""

    list_display = ("pmid", "title", "publication_date")
    search_fields = ("pmid", "title")
    list_filter = ("publication_date",)
    ordering = ("-publication_date",)


@admin.register(PubMedQueryData)
class PubMedQueryDataAdmin(admin.ModelAdmin):
    """Admin configuration for PubMedQueryData model."""

    list_display = ("internal_id", "query", "query_date", "denial_id")
    search_fields = ("query",)
    list_filter = ("query_date",)
    ordering = ("-query_date",)


@admin.register(FaxesToSend)
class FaxesToSendAdmin(admin.ModelAdmin):
    """Admin configuration for FaxesToSend model."""

    list_display = (
        "fax_id",
        "email",
        "date",
        "paid",
        "fax_success",
        "sent",
        "denial_id",
    )
    search_fields = ("email", "name")
    list_filter = ("date", "paid", "fax_success", "sent")
    ordering = ("-date",)
    raw_id_fields = ("denial_id", "for_appeal")
    list_select_related = ("denial_id",)


@admin.register(DenialTypesRelation)
class DenialTypesRelationAdmin(admin.ModelAdmin):
    """Admin configuration for DenialTypesRelation model."""

    list_display = ("denial", "denial_type", "src")
    search_fields = ("denial__denial_text", "denial_type__name")
    ordering = ("denial",)
    raw_id_fields = ("denial",)
    list_select_related = ("denial", "denial_type")


@admin.register(PlanTypesRelation)
class PlanTypesRelationAdmin(admin.ModelAdmin):
    """Admin configuration for PlanTypesRelation model."""

    list_display = ("denial", "plan_type", "src")
    search_fields = ("denial__denial_text", "plan_type__name")
    ordering = ("denial",)
    raw_id_fields = ("denial",)
    list_select_related = ("denial", "plan_type")


@admin.register(PlanSourceRelation)
class PlanSourceRelationAdmin(admin.ModelAdmin):
    """Admin configuration for PlanSourceRelation model."""

    list_display = ("denial", "plan_source", "src")
    search_fields = ("denial__denial_text", "plan_source__name")
    ordering = ("denial",)
    raw_id_fields = ("denial",)
    list_select_related = ("denial", "plan_source")


@admin.register(DenialQA)
class DenialQAAdmin(admin.ModelAdmin):
    """Admin configuration for DenialQA model."""

    list_display = ("id", "denial", "bool_answer")
    search_fields = ("denial__denial_text", "question")
    list_filter = ("bool_answer",)
    ordering = ("id",)
    raw_id_fields = ("denial",)
    list_select_related = ("denial",)


@admin.register(ProposedAppeal)
class ProposedAppealAdmin(admin.ModelAdmin):
    """Admin configuration for ProposedAppeal model."""

    list_display = ("id", "for_denial", "chosen", "editted")
    search_fields = ("appeal_text",)
    list_filter = ("chosen", "editted")
    ordering = ("id",)
    # Use raw_id_fields to avoid loading all Denials in FK dropdown
    raw_id_fields = ("for_denial",)
    # Prefetch related Denial for list view
    list_select_related = ("for_denial",)


@admin.register(Appeal)
class AppealAdmin(admin.ModelAdmin):
    """Admin configuration for Appeal model."""

    list_display = (
        "id",
        "uuid",
        "for_denial",
        "chat",
        "pending",
        "sent",
        "success",
        "creation_date",
    )
    search_fields = ("uuid", "appeal_text")
    list_filter = ("pending", "sent", "success", "creation_date")
    ordering = ("-creation_date",)
    # Use raw_id_fields to avoid loading all Denials in FK dropdown
    raw_id_fields = ("for_denial",)
    # Prefetch related Denial for list view
    list_select_related = ("for_denial",)


@admin.register(SecondaryAppealProfessionalRelation)
class SecondaryAppealProfessionalRelationAdmin(admin.ModelAdmin):
    """Admin configuration for SecondaryAppealProfessionalRelation model."""

    list_display = ("appeal", "professional")
    search_fields = ("appeal__uuid", "professional__user__email")
    ordering = ("appeal",)


@admin.register(SecondaryDenialProfessionalRelation)
class SecondaryDenialProfessionalRelationAdmin(admin.ModelAdmin):
    """Admin configuration for SecondaryDenialProfessionalRelation model."""

    list_display = ("denial", "professional")
    search_fields = ("denial__denial_text", "professional__user__email")
    ordering = ("denial",)


@admin.register(StripeProduct)
class StripeProductAdmin(admin.ModelAdmin):
    """Admin configuration for StripeProduct model."""

    list_display = ("id", "name", "stripe_id", "active")
    search_fields = ("name", "stripe_id")
    list_filter = ("active",)
    ordering = ("id",)


@admin.register(StripePrice)
class StripePriceAdmin(admin.ModelAdmin):
    """Admin configuration for StripePrice model."""

    list_display = ("id", "product", "stripe_id", "amount", "currency", "active")
    search_fields = ("stripe_id",)
    list_filter = ("active", "currency")
    ordering = ("id",)


@admin.register(AppealAttachment)
class AppealAttachmentAdmin(admin.ModelAdmin):
    """Admin configuration for AppealAttachment model."""

    list_display = ("appeal", "filename", "mime_type", "created_at")
    search_fields = ("filename", "mime_type", "appeal__uuid")
    list_filter = ("created_at",)
    ordering = ("-created_at",)


@admin.register(OngoingChat)
class OngoingChatAdmin(admin.ModelAdmin):
    """Admin configuration for OngoingChat model."""

    list_display = (
        "id",
        "professional_user",
        "user",
        "is_patient",
        "denied_item",
        "denied_reason",
        "created_at",
        "updated_at",
    )
    search_fields = (
        "id",
        "denied_item",
        "denied_reason",
        "professional_user__user__email",
        "user__email",
        "session_key",
    )
    list_filter = (
        "is_patient",
        "created_at",
        "updated_at",
        "domain",
    )
    ordering = ("-updated_at",)
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(ChooserTask)
class ChooserTaskAdmin(admin.ModelAdmin):
    """Admin configuration for ChooserTask model."""

    list_display = (
        "id",
        "task_type",
        "status",
        "source",
        "num_candidates_expected",
        "num_candidates_generated",
        "created_at",
    )
    search_fields = ("id", "source")
    list_filter = ("task_type", "status", "created_at")
    ordering = ("-created_at",)
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(ChooserCandidate)
class ChooserCandidateAdmin(admin.ModelAdmin):
    """Admin configuration for ChooserCandidate model."""

    list_display = (
        "id",
        "task",
        "candidate_index",
        "kind",
        "model_name",
        "is_active",
        "created_at",
    )
    search_fields = ("task__id", "model_name", "content")
    list_filter = ("kind", "is_active", "created_at")
    ordering = ("-created_at",)
    readonly_fields = ("id", "created_at")


@admin.register(ChooserVote)
class ChooserVoteAdmin(admin.ModelAdmin):
    """Admin configuration for ChooserVote model."""

    list_display = (
        "id",
        "task",
        "chosen_candidate",
        "session_key",
        "created_at",
    )
    search_fields = ("task__id", "session_key")
    list_filter = ("created_at",)
    ordering = ("-created_at",)
    readonly_fields = ("id", "created_at")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    """Admin configuration for AuditLog model (read-only)."""

    list_display = (
        "id",
        "timestamp",
        "event_type",
        "username",
        "is_professional",
        "path",
        "method",
        "status_code",
        "response_time_ms",
    )
    search_fields = ("username", "path", "event_type", "description")
    list_filter = ("event_type", "is_professional", "method", "timestamp")
    ordering = ("-timestamp",)
    readonly_fields = (
        "id",
        "timestamp",
        "event_type",
        "description",
        "user",
        "username",
        "is_professional",
        "ip_address",
        "user_agent",
        "path",
        "method",
        "status_code",
        "response_time_ms",
        "extra_data",
    )
    date_hierarchy = "timestamp"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(FuzzAttempt)
class FuzzAttemptAdmin(admin.ModelAdmin):
    """
    Admin configuration for FuzzAttempt model.

    Provides investigation interface for detected fuzzing/scanning attempts.
    All fields are readonly - records are created by middleware only.
    """

    list_display = (
        "id",
        "created_at",
        "ip_hash_short",
        "ip_prefix",
        "method",
        "path_truncated",
        "score",
        "status_returned",
        "is_authenticated",
    )

    list_filter = (
        "created_at",
        "status_returned",
        "is_authenticated",
        "method",
    )

    search_fields = (
        "ip_hash",
        "ip_prefix",
        "path",
        "reason",
        "request_id",
        "session_key",
    )

    readonly_fields = (
        "id",
        "created_at",
        "ip_hash",
        "ip_prefix",
        "raw_ip",
        "user",
        "session_key",
        "is_authenticated",
        "method",
        "path",
        "status_returned",
        "reason_formatted",
        "score",
        "request_id",
        "encrypted_blob_size",
        "key_version",
        "decrypted_preview",
    )

    ordering = ("-created_at",)
    date_hierarchy = "created_at"

    fieldsets = (
        (
            "Request Info",
            {
                "fields": (
                    "created_at",
                    "method",
                    "path",
                    "status_returned",
                    "request_id",
                )
            },
        ),
        (
            "Detection",
            {
                "fields": ("score", "reason_formatted"),
            },
        ),
        (
            "Client Info",
            {
                "fields": (
                    "ip_hash",
                    "ip_prefix",
                    "raw_ip",
                    "user",
                    "session_key",
                    "is_authenticated",
                ),
            },
        ),
        (
            "Encrypted Capture",
            {
                "fields": ("encrypted_blob_size", "key_version", "decrypted_preview"),
                "classes": ("collapse",),
            },
        ),
    )

    actions = ["purge_selected", "export_metadata_csv"]

    @admin.display(description="IP Hash")
    def ip_hash_short(self, obj):
        """Show truncated IP hash."""
        return f"{obj.ip_hash[:16]}..."

    @admin.display(description="Path")
    def path_truncated(self, obj):
        """Show truncated path."""
        max_len = 50
        if len(obj.path) > max_len:
            return obj.path[:max_len] + "..."
        return obj.path

    @admin.display(description="Reasons")
    def reason_formatted(self, obj):
        """Format reasons as readable list."""
        from django.utils.html import escape, format_html
        import json

        try:
            reasons = json.loads(obj.reason)
            if isinstance(reasons, list):
                # Escape each item to prevent XSS
                items = "".join(f"<li>{escape(str(r))}</li>" for r in reasons)
                return format_html(
                    "<ul style='margin:0;padding-left:20px;'>{}</ul>",
                    format_html(items),
                )
        except (json.JSONDecodeError, TypeError):
            pass
        return obj.reason

    @admin.display(description="Capture Preview")
    def decrypted_preview(self, obj):
        """
        Show encrypted capture preview.

        Note: Actual decryption requires view_fuzz_capture permission
        and is only shown to authorized users.
        """
        from django.utils.html import format_html

        if not obj.encrypted_blob:
            return "No capture available"

        warning = (
            '<div style="background:#fff3cd;border:1px solid #ffc107;'
            'padding:10px;margin-bottom:10px;border-radius:4px;">'
            "<strong>Warning:</strong> Encrypted blob may contain sensitive data "
            "(Authorization headers, cookies, etc.). Handle with care."
            "</div>"
        )

        info = (
            f"<pre style='max-width:600px;overflow:auto;background:#f5f5f5;padding:10px;'>"
            f"Blob Size: {obj.encrypted_blob_size or 0} bytes\n"
            f"Key Version: {obj.key_version}\n\n"
            f"[Full decryption requires view_fuzz_capture permission\n"
            f"and is available via management commands]"
            f"</pre>"
        )

        return format_html(warning + info)

    @admin.action(description="Purge selected fuzz attempts")
    def purge_selected(self, request, queryset):
        """Admin action to purge selected fuzz attempts."""
        count = queryset.count()
        # Delete associated files first
        for obj in queryset:
            if obj.encrypted_blob:
                try:
                    obj.encrypted_blob.delete(save=False)
                except Exception as e:
                    logger.warning(f"Failed to delete encrypted_blob for FuzzAttempt {obj.id}: {e}")
        queryset.delete()
        self.message_user(request, f"Purged {count} fuzz attempt records.")

    @admin.action(description="Export metadata as CSV")
    def export_metadata_csv(self, request, queryset):
        """Export selected records as CSV (metadata only, no decrypted content)."""
        import csv
        from django.http import HttpResponse

        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="fuzz_attempts.csv"'

        writer = csv.writer(response)
        writer.writerow(
            [
                "id",
                "created_at",
                "ip_prefix",
                "method",
                "path",
                "score",
                "status_returned",
                "reason",
            ]
        )

        for obj in queryset:
            writer.writerow(
                [
                    obj.id,
                    obj.created_at.isoformat(),
                    obj.ip_prefix,
                    obj.method,
                    obj.path[:200],
                    obj.score,
                    obj.status_returned,
                    obj.reason[:500],
                ]
            )

        return response

    def has_add_permission(self, request):
        """Prevent manual record creation."""
        return False

    def has_change_permission(self, request, obj=None):
        """Prevent record modification."""
        return False
