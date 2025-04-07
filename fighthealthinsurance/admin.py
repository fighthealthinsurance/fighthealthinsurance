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
)
from fhi_users.models import (
    ProfessionalUser,
    PatientUser,
    UserDomain,
    ProfessionalDomainRelation,
)
from django.contrib.auth.admin import UserAdmin


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
    )
    search_fields = ("raw_email", "denial_text", "insurance_company")
    list_filter = (
        ("raw_email", admin.EmptyFieldListFilter),
        "plan_source__name",
        "plan_type__name",
        "denial_type__name",
        "date",
        ("appeal_text", admin.EmptyFieldListFilter),
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

    list_display = ("id", "email", "name", "signup_date")
    search_fields = ("email", "name")
    list_filter = ("signup_date",)
    ordering = ("-signup_date",)


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
