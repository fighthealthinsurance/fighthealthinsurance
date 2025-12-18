from django.contrib import admin
from fighthealthinsurance.models import (
    Denial,
    InterestedProfessional,
    MailingListSubscriber,
    FollowUpType,
    FollowUp,
    FollowUpSched,
    PlanType,
    Regulator,
    PlanSource,
    Diagnosis,
    Procedures,
    DenialTypes,
    AppealTemplates,
    DataSource,
    PlanDocuments,
    FollowUpDocuments,
    PubMedArticleSummarized,
    PubMedQueryData,
    FaxesToSend,
    DenialTypesRelation,
    PlanTypesRelation,
    PlanSourceRelation,
    DenialQA,
    ProposedAppeal,
    Appeal,
    SecondaryAppealProfessionalRelation,
    SecondaryDenialProfessionalRelation,
    StripeProduct,
    StripePrice,
    AppealAttachment,
    GenericContextGeneration,
    GenericQuestionGeneration,
    PriorAuthRequest,
    ProposedPriorAuth,
    OngoingChat,
    ChatLeads,
    ChooserTask,
    ChooserCandidate,
    ChooserVote,
)
from fhi_users.models import (
    ProfessionalUser,
    PatientUser,
    UserDomain,
    ProfessionalDomainRelation,
)
from fhi_users.audit_models import (
    AuthAuditLog,
    APIAccessLog,
    ProfessionalActivityLog,
    SuspiciousActivityLog,
    ObjectActivityContext,
)
from django.contrib.auth.admin import UserAdmin


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
        "patient_visible",
        "appeal_result",
        "referral_source",
    )
    search_fields = (
        "raw_email",
        "denial_text",
        "insurance_company",
        "referral_source",
        "referral_source_details",
     )
    list_filter = (
        ("raw_email", admin.EmptyFieldListFilter),
        "plan_source__name",
        "plan_type__name",
        "denial_type__name",
        "date",
        ("appeal_text", admin.EmptyFieldListFilter),
        "referral_source",
    )
    ordering = ("-date",)


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


@admin.register(DenialTypesRelation)
class DenialTypesRelationAdmin(admin.ModelAdmin):
    """Admin configuration for DenialTypesRelation model."""

    list_display = ("denial", "denial_type", "src")
    search_fields = ("denial__denial_text", "denial_type__name")
    ordering = ("denial",)


@admin.register(PlanTypesRelation)
class PlanTypesRelationAdmin(admin.ModelAdmin):
    """Admin configuration for PlanTypesRelation model."""

    list_display = ("denial", "plan_type", "src")
    search_fields = ("denial__denial_text", "plan_type__name")
    ordering = ("denial",)


@admin.register(PlanSourceRelation)
class PlanSourceRelationAdmin(admin.ModelAdmin):
    """Admin configuration for PlanSourceRelation model."""

    list_display = ("denial", "plan_source", "src")
    search_fields = ("denial__denial_text", "plan_source__name")
    ordering = ("denial",)


@admin.register(DenialQA)
class DenialQAAdmin(admin.ModelAdmin):
    """Admin configuration for DenialQA model."""

    list_display = ("id", "denial", "bool_answer")
    search_fields = ("denial__denial_text", "question")
    list_filter = ("bool_answer",)
    ordering = ("id",)


@admin.register(ProposedAppeal)
class ProposedAppealAdmin(admin.ModelAdmin):
    """Admin configuration for ProposedAppeal model."""

    list_display = ("id", "for_denial", "chosen", "editted")
    search_fields = ("appeal_text",)
    list_filter = ("chosen", "editted")
    ordering = ("id",)


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


# =============================================================================
# Audit Log Admin Classes
# =============================================================================


@admin.register(AuthAuditLog)
class AuthAuditLogAdmin(admin.ModelAdmin):
    """Admin configuration for authentication audit logs."""

    list_display = (
        "timestamp",
        "event_type",
        "user",
        "success",
        "ip_address",
        "asn_org",
        "network_type",
        "country_code",
        "user_agent_browser",
    )
    list_filter = (
        "event_type",
        "success",
        "network_type",
        "country_code",
        "timestamp",
    )
    search_fields = (
        "user__email",
        "user__username",
        "attempted_email",
        "ip_address",
        "asn_org",
    )
    ordering = ("-timestamp",)
    readonly_fields = (
        "id",
        "timestamp",
        "user",
        "user_type",
        "professional_user",
        "domain",
        "event_type",
        "success",
        "ip_address",
        "asn_number",
        "asn_org",
        "network_type",
        "country_code",
        "state_region",
        "user_agent_full",
        "user_agent_browser",
        "user_agent_os",
        "is_mobile",
        "is_bot",
        "request_path",
        "request_method",
        "http_referer",
        "session_key",
        "attempted_email",
        "failure_reason",
        "details",
    )
    date_hierarchy = "timestamp"

    def has_add_permission(self, request):
        """
        Prevent creation of new model instances via the admin interface.

        Returns:
            `False` to disallow adding objects through the admin.
        """
        return False

    def has_change_permission(self, request, obj=None):
        """
        Prevent all change operations through the admin interface.

        Parameters:
            request: The current HttpRequest (ignored).
            obj: The model instance to check permission for; may be None.

        Returns:
            False: Always denies change permission.
        """
        return False

    def has_delete_permission(self, request, obj=None):
        # Allow superusers to delete for data retention
        """
        Determine whether the requesting user is permitted to delete the given object.

        Parameters:
            request (HttpRequest): The current request used to inspect the requesting user.
            obj (Model | None): The object to check permission for (unused).

        Returns:
            bool: True if the requesting user is a superuser, False otherwise.
        """
        return request.user.is_superuser


@admin.register(APIAccessLog)
class APIAccessLogAdmin(admin.ModelAdmin):
    """Admin configuration for API access audit logs."""

    list_display = (
        "timestamp",
        "user",
        "endpoint",
        "request_method",
        "http_status",
        "response_time_ms",
        "network_type",
        "ip_address",
    )
    list_filter = (
        "request_method",
        "http_status",
        "network_type",
        "timestamp",
    )
    search_fields = (
        "user__email",
        "endpoint",
        "ip_address",
        "asn_org",
        "resource_type",
        "search_query",
    )
    ordering = ("-timestamp",)
    readonly_fields = (
        "id",
        "timestamp",
        "user",
        "user_type",
        "professional_user",
        "domain",
        "event_type",
        "success",
        "ip_address",
        "asn_number",
        "asn_org",
        "network_type",
        "country_code",
        "state_region",
        "user_agent_full",
        "user_agent_browser",
        "user_agent_os",
        "is_mobile",
        "is_bot",
        "request_path",
        "request_method",
        "http_referer",
        "session_key",
        "endpoint",
        "http_status",
        "response_time_ms",
        "resource_type",
        "resource_id",
        "resource_count",
        "search_query",
        "details",
    )
    date_hierarchy = "timestamp"

    def has_add_permission(self, request):
        """
        Prevent creation of new model instances via the admin interface.

        Returns:
            `False` to disallow adding objects through the admin.
        """
        return False

    def has_change_permission(self, request, obj=None):
        """
        Prevent all change operations through the admin interface.

        Parameters:
            request: The current HttpRequest (ignored).
            obj: The model instance to check permission for; may be None.

        Returns:
            False: Always denies change permission.
        """
        return False

    def has_delete_permission(self, request, obj=None):
        """
        Allow deletion only for superusers.

        Parameters:
            request (HttpRequest): The incoming admin request from the user attempting the action.
            obj (Model, optional): The model instance targeted for deletion, if any.

        Returns:
            bool: `true` if the requesting user is a superuser, `false` otherwise.
        """
        return request.user.is_superuser


@admin.register(ProfessionalActivityLog)
class ProfessionalActivityLogAdmin(admin.ModelAdmin):
    """Admin configuration for professional activity audit logs."""

    list_display = (
        "timestamp",
        "event_type",
        "performed_by_user",
        "target_user",
        "domain",
        "success",
        "ip_address",
    )
    list_filter = (
        "event_type",
        "success",
        "timestamp",
    )
    search_fields = (
        "performed_by_user__email",
        "target_user__email",
        "domain__name",
        "ip_address",
    )
    ordering = ("-timestamp",)
    readonly_fields = (
        "id",
        "timestamp",
        "user",
        "user_type",
        "professional_user",
        "domain",
        "event_type",
        "success",
        "ip_address",
        "asn_number",
        "asn_org",
        "network_type",
        "country_code",
        "state_region",
        "user_agent_full",
        "user_agent_browser",
        "user_agent_os",
        "is_mobile",
        "is_bot",
        "request_path",
        "request_method",
        "http_referer",
        "session_key",
        "performed_by_user",
        "performed_by_professional",
        "target_user",
        "target_professional",
        "old_values",
        "new_values",
        "details",
    )
    date_hierarchy = "timestamp"

    def has_add_permission(self, request):
        """
        Prevent creation of new model instances via the admin interface.

        Returns:
            `False` to disallow adding objects through the admin.
        """
        return False

    def has_change_permission(self, request, obj=None):
        """
        Prevent all change operations through the admin interface.

        Parameters:
            request: The current HttpRequest (ignored).
            obj: The model instance to check permission for; may be None.

        Returns:
            False: Always denies change permission.
        """
        return False

    def has_delete_permission(self, request, obj=None):
        """
        Allow deletion only for superusers.

        Parameters:
            request (HttpRequest): The incoming admin request from the user attempting the action.
            obj (Model, optional): The model instance targeted for deletion, if any.

        Returns:
            bool: `true` if the requesting user is a superuser, `false` otherwise.
        """
        return request.user.is_superuser


@admin.register(SuspiciousActivityLog)
class SuspiciousActivityLogAdmin(admin.ModelAdmin):
    """Admin configuration for suspicious activity flags."""

    list_display = (
        "timestamp",
        "severity",
        "trigger_type",
        "user",
        "ip_address",
        "asn_org",
        "reviewed",
    )
    list_filter = (
        "severity",
        "trigger_type",
        "reviewed",
        "timestamp",
    )
    search_fields = (
        "user__email",
        "ip_address",
        "asn_org",
        "description",
    )
    ordering = ("-timestamp",)
    readonly_fields = (
        "id",
        "timestamp",
        "trigger_type",
        "severity",
        "user",
        "professional_user",
        "domain",
        "ip_address",
        "asn_number",
        "asn_org",
        "description",
        "evidence",
    )
    # These fields can be edited for resolution tracking
    fields = (
        "timestamp",
        "severity",
        "trigger_type",
        "user",
        "professional_user",
        "domain",
        "ip_address",
        "asn_number",
        "asn_org",
        "description",
        "evidence",
        "reviewed",
        "reviewed_by",
        "reviewed_at",
        "resolution_notes",
    )
    date_hierarchy = "timestamp"

    def has_add_permission(self, request):
        """
        Prevent creation of new model instances via the admin interface.

        Returns:
            `False` to disallow adding objects through the admin.
        """
        return False

    def has_delete_permission(self, request, obj=None):
        """
        Allow deletion only for superusers.

        Parameters:
            request (HttpRequest): The incoming admin request from the user attempting the action.
            obj (Model, optional): The model instance targeted for deletion, if any.

        Returns:
            bool: `true` if the requesting user is a superuser, `false` otherwise.
        """
        return request.user.is_superuser


@admin.register(ObjectActivityContext)
class ObjectActivityContextAdmin(admin.ModelAdmin):
    """Admin configuration for object activity context (linked to Denials, Appeals, etc.)."""

    list_display = (
        "timestamp",
        "action",
        "content_type",
        "object_id",
        "user",
        "user_type",
        "ip_address",
        "user_agent_browser",
        "network_type",
    )
    list_filter = (
        "action",
        "user_type",
        "network_type",
        "content_type",
        "timestamp",
    )
    search_fields = (
        "user__email",
        "object_id",
        "ip_address",
        "asn_org",
    )
    ordering = ("-timestamp",)
    readonly_fields = (
        "id",
        "timestamp",
        "content_type",
        "object_id",
        "action",
        "user",
        "user_type",
        "professional_user",
        "domain",
        "ip_address",
        "asn_number",
        "asn_org",
        "network_type",
        "country_code",
        "state_region",
        "user_agent_full",
        "user_agent_browser",
        "user_agent_os",
        "is_mobile",
        "is_bot",
        "session_key",
    )
    date_hierarchy = "timestamp"

    def has_add_permission(self, request):
        """
        Prevent creation of new model instances via the admin interface.

        Returns:
            `False` to disallow adding objects through the admin.
        """
        return False

    def has_change_permission(self, request, obj=None):
        """
        Prevent all change operations through the admin interface.

        Parameters:
            request: The current HttpRequest (ignored).
            obj: The model instance to check permission for; may be None.

        Returns:
            False: Always denies change permission.
        """
        return False

    def has_delete_permission(self, request, obj=None):
        """
        Allow deletion only for superusers.

        Parameters:
            request (HttpRequest): The incoming admin request from the user attempting the action.
            obj (Model, optional): The model instance targeted for deletion, if any.

        Returns:
            bool: `true` if the requesting user is a superuser, `false` otherwise.
        """
        return request.user.is_superuser
