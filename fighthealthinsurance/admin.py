import csv
from typing import Optional
from urllib.parse import urlsplit

from django import forms
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin
from django.core.exceptions import PermissionDenied
from django.db import connection, transaction
from django.db.models import Count, DateTimeField, IntegerField
from django.db.models.expressions import RawSQL
from django.http import (
    HttpResponse,
    HttpResponseNotAllowed,
    HttpResponseRedirect,
    JsonResponse,
)
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html
from loguru import logger

from fighthealthinsurance.constants import SITE_BANNER_CANNED_MESSAGES
from fighthealthinsurance.templatetags.bundles import versioned

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
    ChatTurn,
    ChooserCandidate,
    ChooserTask,
    ChooserVote,
    PaidReviewBounty,
    PaidReviewSession,
    DataSource,
    Denial,
    DenialQA,
    DenialTypes,
    DenialTypesRelation,
    Diagnosis,
    FaxesToSend,
    HumanJudgeSubmission,
    HumanJudgeVerdict,
    KnownNonFaxNumber,
    FollowUp,
    FollowUpDocuments,
    FollowUpSched,
    FollowUpType,
    GenericContextGeneration,
    GenericQuestionGeneration,
    InsuranceCompany,
    InsurancePlan,
    InterestedProfessional,
    PayerPriorAuthRequirement,
    MailingListSubscriber,
    MedicationContext,
    OngoingChat,
    PayerPolicyEntry,
    PlanDocumentMatch,
    PlanDocuments,
    PlanSource,
    PlanSponsor,
    PlanSourceRelation,
    PlanType,
    PlanTypesRelation,
    PriorAuthRequest,
    Procedures,
    ProfessionalInquiry,
    ProposedAppeal,
    ProposedPriorAuth,
    PublicPlanDocument,
    PubMedArticleSummarized,
    PubMedQueryData,
    Regulator,
    SiteBanner,
    SecondaryAppealProfessionalRelation,
    SecondaryDenialProfessionalRelation,
    StripePrice,
    StripeProduct,
    PolicyDocument,
    PolicyDocumentAnalysis,
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

    # Triage is machine-written and only meaningful for the letter it was
    # computed from (ml/denial_triage.is_current), so the raw columns are
    # kept out of the change form entirely (nobody edits a model's answer by
    # hand) and staff see one read-only, current-gated summary instead; a
    # result that landed for a replaced letter reads as blank, never as
    # letter B's.
    exclude = tuple(
        column
        for column in (
            "triage_category",
            "triage_category_confidence",
            "triage_regulation",
            "triage_regulation_confidence",
            "triage_pre_service",
            "triage_urgent",
            "triage_regulator",
            "appeal_deadline",
            "appeal_deadline_label",
            "appeal_deadline_confidence",
            "triage_source",
            "triage_text_hash",
            "triaged_at",
            # Set by the two deadline actions below, never typed.
            "appeal_deadline_check",
        )
    )
    readonly_fields = ("triage_summary",)
    actions = ("mark_deadline_correct", "mark_deadline_wrong")

    def _label_deadline(self, request, queryset, verdict: str) -> None:
        """Record a person's verdict on the triage deadline of each selected
        denial that has a current one; the tally on the model dashboard's
        Jev panel decides when readers may be told deadlines."""
        from fighthealthinsurance.ml import denial_triage

        ids = [
            denial.pk
            for denial in queryset.exclude(appeal_deadline_label__isnull=True)
            if denial_triage.is_current(denial)
        ]
        updated = Denial.objects.filter(pk__in=ids).update(
            appeal_deadline_check=verdict
        )
        self.message_user(
            request,
            f"Marked {updated} deadline(s) {verdict}; "
            f"{queryset.count() - updated} had no current triage deadline.",
        )

    @admin.action(description="Triage deadline is correct")
    def mark_deadline_correct(self, request, queryset):
        from fighthealthinsurance.ml import denial_triage

        self._label_deadline(request, queryset, denial_triage.DEADLINE_CORRECT)

    @admin.action(description="Triage deadline is wrong")
    def mark_deadline_wrong(self, request, queryset):
        from fighthealthinsurance.ml import denial_triage

        self._label_deadline(request, queryset, denial_triage.DEADLINE_WRONG)

    def save_model(self, request, obj, form, change):  # type: ignore[override]
        # The triage columns are not on the form, but denial_date is, and an
        # anchored window ("180 days from notice") is counted from it: keep
        # the resolved date in step with the edited one, for a current
        # triage only (same rule as FindNextStepsHelper.find_next_steps).
        from fighthealthinsurance.ml import denial_triage

        if (
            change
            and denial_triage.is_anchored_window(obj.appeal_deadline_label)
            and denial_triage.is_current(obj)
        ):
            obj.appeal_deadline = denial_triage.resolve_window(
                obj.appeal_deadline_label, obj.denial_date
            )
        super().save_model(request, obj, form, change)

    @admin.display(description="Triage (TypeSafe, current letter only)")
    def triage_summary(self, obj: Denial) -> str:
        from fighthealthinsurance.ml import denial_triage

        if not denial_triage.is_current(obj):
            return ""
        parts = [
            f"category={obj.triage_category} ({(obj.triage_category_confidence or 0):.2f})",
            f"regulation={obj.triage_regulation} ({(obj.triage_regulation_confidence or 0):.2f})",
            f"pre_service={(obj.triage_pre_service or 0):.2f}",
            f"urgent={(obj.triage_urgent or 0):.2f}",
            (
                f"regulator={obj.triage_regulator:.2f}"
                if obj.triage_regulator is not None
                else ""
            ),
            f"deadline={self.appeal_deadline_current(obj) or 'none'}",
            (
                f"deadline_check={obj.appeal_deadline_check}"
                if obj.appeal_deadline_check
                else ""
            ),
            f"triaged_at={obj.triaged_at:%Y-%m-%d %H:%M}" if obj.triaged_at else "",
        ]
        return "; ".join(part for part in parts if part)

    @admin.display(description="Triage category")
    def triage_category_current(self, obj: Denial) -> str:
        from fighthealthinsurance.ml import denial_triage

        if not denial_triage.is_current(obj):
            return ""
        return obj.triage_category or ""

    @admin.display(description="Appeal deadline (triage)")
    def appeal_deadline_current(self, obj: Denial) -> str:
        from fighthealthinsurance.ml import denial_triage

        if not denial_triage.is_current(obj) or not obj.appeal_deadline_label:
            return ""
        when = obj.appeal_deadline.isoformat() if obj.appeal_deadline else "unresolved"
        confidence = (
            f"{obj.appeal_deadline_confidence:.2f}"
            if obj.appeal_deadline_confidence is not None
            else "?"
        )
        return f"{when} ({obj.appeal_deadline_label}, conf {confidence})"

    list_display = (
        "denial_id",
        "date",
        "raw_email",
        "insurance_company_obj",
        "insurance_plan_obj",
        "patient_visible",
        "appeal_result",
        "referral_source",
        "triage_category_current",
        "appeal_deadline_current",
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


def _submissions_link(lead_id: int, count: int):
    url = reverse("admin:fighthealthinsurance_professionalinquiry_changelist")
    return format_html(
        '<a href="{}?interested_professional__id__exact={}">{}</a>', url, lead_id, count
    )


@admin.register(InterestedProfessional)
class InterestedProfessionalAdmin(admin.ModelAdmin):
    """Admin configuration for InterestedProfessional model."""

    list_display = (
        "id",
        "name",
        "email",
        "paid",
        "signup_date",
        "thankyou_email_sent",
        "blocked",
        "submissions",
    )
    search_fields = ("name", "email", "business_name")
    list_filter = ("paid", "blocked", "thankyou_email_sent", "signup_date", "mod_date")
    ordering = ("-signup_date",)
    readonly_fields = ("submissions",)

    def get_queryset(self, request):
        return (
            super().get_queryset(request).annotate(submission_count=Count("inquiries"))
        )

    @admin.display(description="Submissions", ordering="submission_count")
    def submissions(self, obj: InterestedProfessional):
        count = getattr(obj, "submission_count", None)
        if count is None:
            count = obj.inquiries.count()
        return _submissions_link(obj.id, count)


@admin.register(ProfessionalInquiry)
class ProfessionalInquiryAdmin(admin.ModelAdmin):
    """Every professional interest form submission (see pro_signups.py).

    A log: rows are read-only. A person is the linked lead, or the email when
    no lead is linked; their count and dates cover all their submissions.
    """

    list_display = (
        "created_at",
        "email",
        "name",
        "business_name",
        "outcome",
        "spam_reason",
        "person_submissions",
        "person_first",
        "person_last",
        "lead",
        "source",
    )
    list_filter = ("outcome", "source", "created_at")
    search_fields = ("email", "name", "business_name", "phone_number")
    ordering = ("-created_at",)
    actions = ["mark_not_spam"]
    readonly_fields = (
        "interested_professional",
        "name",
        "email",
        "phone_number",
        "phone_digits",
        "business_name",
        "message",
        "source",
        "created_at",
        "outcome",
        "spam_reason",
        "created_lead",
    )

    def has_add_permission(self, request):
        return False

    def get_queryset(self, request):
        from fighthealthinsurance.pro_signups import person_aggregate

        return (
            super()
            .get_queryset(request)
            .select_related("interested_professional")
            .annotate(
                person_count=person_aggregate("COUNT", "id", IntegerField()),
                person_first_at=person_aggregate("MIN", "created_at", DateTimeField()),
                person_last_at=person_aggregate("MAX", "created_at", DateTimeField()),
            )
        )

    @admin.display(description="Their submissions", ordering="person_count")
    def person_submissions(self, obj: ProfessionalInquiry):
        return getattr(obj, "person_count", None)

    @admin.display(description="First", ordering="person_first_at")
    def person_first(self, obj: ProfessionalInquiry):
        return getattr(obj, "person_first_at", None)

    @admin.display(description="Last", ordering="person_last_at")
    def person_last(self, obj: ProfessionalInquiry):
        return getattr(obj, "person_last_at", None)

    @admin.display(description="Lead")
    def lead(self, obj: ProfessionalInquiry):
        if obj.interested_professional_id is None:
            return "-"
        url = reverse(
            "admin:fighthealthinsurance_interestedprofessional_change",
            args=[obj.interested_professional_id],
        )
        return format_html('<a href="{}">#{}</a>', url, obj.interested_professional_id)

    @admin.action(description="Not spam: add as a lead (sends nothing)")
    def mark_not_spam(self, request, queryset):
        from fighthealthinsurance.pro_signups import mark_not_spam

        cleared = sum(1 for inquiry in queryset if mark_not_spam(inquiry))
        self.message_user(
            request,
            f"Marked {cleared} submission(s) as not spam. Nothing was sent.",
        )


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

    list_display = ("id", "name", "template_name", "subject", "duration")
    search_fields = ("name", "template_name", "subject", "text")
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

    list_display = ("id", "name", "website", "alt_name", "phone")
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

    list_display = (
        "id",
        "name",
        "website",
        "appeal_fax_number",
        "is_tpa",
        "is_marketplace_focused",
    )
    list_filter = ("is_tpa", "is_marketplace_focused")
    search_fields = ("name", "alt_names")
    ordering = ("name",)
    autocomplete_fields = ["parent_company"]
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "name",
                    "alt_names",
                    "website",
                    "member_services_url",
                    "parent_company",
                    "notes",
                ),
            },
        ),
        (
            "Appeal Routing",
            {
                "fields": (
                    "appeal_address",
                    "appeal_fax_number",
                    "appeal_phone_number",
                    "appeal_email",
                    "appeals_portal_url",
                    "appeals_info_url",
                ),
                "description": (
                    "Where appeals get sent. Used to populate "
                    "Denial.appeal_fax_number and to suggest portals/addresses."
                ),
            },
        ),
        (
            "Company Type",
            {
                "fields": (
                    "is_tpa",
                    "is_marketplace_focused",
                    "is_major_commercial_payer",
                ),
                "description": "Flags to indicate company type for suggestions",
            },
        ),
        (
            "Public Medical Policy",
            {
                "fields": (
                    "medical_policy_url",
                    "medical_policy_url_is_static_index",
                    "medical_policy_name",
                    "medical_policy_notes",
                ),
                "description": (
                    "Public-facing medical/coverage-policy index for this "
                    "payer (e.g., Aetna's Clinical Policy Bulletins). Used "
                    "as comparative evidence in appeal generation; not "
                    "binding on a member's specific plan. Set "
                    "'static index' true only when the URL is a "
                    "directly-browsable HTML or PDF index, not a search "
                    "portal or state-selection page."
                ),
            },
        ),
        (
            "Prior Authorization Requirement List",
            {
                "fields": (
                    "pa_requirement_list_url",
                    "pa_requirement_list_url_is_parseable",
                    "pa_requirement_list_notes",
                ),
                "description": (
                    "URL to the payer's published PA requirement list "
                    "(Excel/PDF/HTML). Set 'is parseable' true once the "
                    "response Content-Type is covered by "
                    "pa_requirement_parsers.PARSERS_BY_CONTENT_TYPE. Run "
                    "'manage.py ingest_pa_requirements' to auto-import "
                    "rows into PayerPriorAuthRequirement."
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


@admin.register(PayerPolicyEntry)
class PayerPolicyEntryAdmin(admin.ModelAdmin):
    """Read-only admin for entries written by ingest_payer_policy_indexes."""

    list_display = (
        "insurance_company",
        "title",
        "payer_policy_id",
        "url",
        "last_indexed",
    )
    list_filter = ("insurance_company",)
    search_fields = ("title", "payer_policy_id", "url")
    ordering = ("insurance_company__name", "title")
    readonly_fields = ("last_indexed",)
    autocomplete_fields = ["insurance_company"]

    def has_add_permission(self, request):  # type: ignore[override]
        return False

    def has_change_permission(self, request, obj=None):  # type: ignore[override]
        return False


class PublicPlanDocumentForm(forms.ModelForm):
    """The add/change form: the text is never edited by hand; a file
    uploaded here is parsed the way the importer parses one, and a new
    row needs one."""

    upload = forms.FileField(
        required=False,
        help_text=(
            "A PDF, Word or text file of the document, up to 20MB; parsed on "
            "save and replaces the stored text. Bulk loads go through "
            "import_plan_documents."
        ),
    )

    class Meta:
        model = PublicPlanDocument
        exclude = ("text",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # A RegexField holds a compiled pattern; shown as is, the form would
        # offer its repr and write that back as the pattern on save.
        for name in ("match_regex", "negative_regex"):
            value = self.initial.get(name)
            if value is not None and not isinstance(value, str):
                self.initial[name] = getattr(value, "pattern", "")
        #: What the upload parsed to, for save_model: (text, page_count).
        self.parsed_upload: Optional[tuple[str, int]] = None

    def clean_upload(self):
        upload = self.cleaned_data.get("upload")
        if upload:
            from fighthealthinsurance.plan_document_bundle import (
                RecordError,
                page_marked_text_from_bytes,
            )
            from fighthealthinsurance.upload_limits import validate_policy_document

            # The policy-document checks: extension, 20MB, PDF/DOCX bytes.
            validate_policy_document(upload)
            upload.seek(0)
            try:
                self.parsed_upload = page_marked_text_from_bytes(
                    upload.read(), upload.name or ""
                )
            except RecordError as exc:
                raise forms.ValidationError(str(exc)) from None
        return upload

    def clean(self):
        cleaned = super().clean() or {}
        if self.instance.pk is None and not cleaned.get("upload"):
            self.add_error("upload", "A new document needs its file.")
        company = cleaned.get("insurance_company")
        plan = cleaned.get("insurance_plan")
        if company and plan and plan.insurance_company_id != company.pk:
            self.add_error("insurance_plan", "That plan belongs to another insurer.")
        for name in ("plan_id_prefixes", "match_keywords"):
            value = cleaned.get(name)
            if value in (None, ""):
                continue
            if not isinstance(value, list) or not all(
                isinstance(v, str) for v in value
            ):
                self.add_error(name, 'A JSON list of strings, such as ["GRP"].')
        return cleaned


@admin.register(PublicPlanDocument)
class PublicPlanDocumentAdmin(admin.ModelAdmin):
    """The public plan documents we fetched (plan_document_matcher): rows
    from import_plan_documents, or added here one at a time."""

    form = PublicPlanDocumentForm
    list_display = (
        "document_key",
        "insurance_company",
        "title",
        "document_type",
        "coverage_kind",
        "line_of_business",
        "state",
        "region",
        "plan_year",
        "page_count",
        "is_active",
        "updated_at",
    )
    list_select_related = ("insurance_company",)
    list_filter = (
        "insurance_company",
        "document_type",
        "coverage_kind",
        "line_of_business",
        "state",
        "is_active",
        "plan_year",
    )
    search_fields = ("document_key", "title", "region", "employer_name", "source_url")
    ordering = ("insurance_company__name", "-plan_year", "title")
    autocomplete_fields = ["insurance_company", "insurance_plan"]
    readonly_fields = (
        "read_text",
        "page_count",
        "char_count",
        "text_sha256",
        "source_sha256",
        "import_batch",
        "pipeline_version",
        "created_at",
        "updated_at",
    )
    actions = ("activate", "deactivate")
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "document_key",
                    "insurance_company",
                    "insurance_plan",
                    "title",
                    "document_type",
                    "coverage_kind",
                    "line_of_business",
                    "is_active",
                    "notes",
                ),
            },
        ),
        (
            "Matching",
            {
                "fields": (
                    "state",
                    "region",
                    "employer_name",
                    "plan_year",
                    "plan_id_prefixes",
                    "match_keywords",
                    "match_regex",
                    "negative_regex",
                ),
                "description": (
                    "What places a denial letter with this document. Keywords "
                    "are matched as whole words; a negative regex rules the "
                    "document out. Keep both away from carrier letterhead and "
                    "grievance addresses, which appear on every letter."
                ),
            },
        ),
        (
            "Source",
            {
                "fields": (
                    "source_url",
                    "fetched_at",
                    "source_sha256",
                    "stored_file",
                    "pipeline_version",
                    "import_batch",
                    "created_at",
                    "updated_at",
                ),
            },
        ),
        (
            "Text",
            {
                # Never the text field itself (it runs to megabytes and the
                # form excludes it): the upload that fills it and the link
                # that shows it.
                "fields": (
                    "upload",
                    "read_text",
                    "page_count",
                    "char_count",
                    "text_sha256",
                ),
            },
        ),
    )

    def get_queryset(self, request):
        # The list never shows the text, which runs to megabytes a row.
        return super().get_queryset(request).defer("text")

    def save_model(self, request, obj, form, change):
        parsed = getattr(form, "parsed_upload", None)
        if parsed:
            text, page_count = parsed
            obj.text = text
            obj.text_sha256 = PublicPlanDocument.text_hash(text)
            obj.page_count = page_count
            obj.char_count = len(text)
            obj.import_batch = "admin"
        super().save_model(request, obj, form, change)
        if parsed and change:
            # The passages built from the old text no longer quote it.
            PlanDocumentMatch.objects.filter(document=obj).delete()

    @admin.display(description="Text")
    def read_text(self, obj: PublicPlanDocument) -> str:
        if not obj.pk or not obj.char_count:
            return "-"
        return format_html(
            '<a href="{}">Read the text</a> ({} pages, {} characters)',
            reverse("staff_plan_document_corpus_text", kwargs={"pk": obj.pk}),
            obj.page_count,
            obj.char_count,
        )

    @admin.action(description="Activate: match these documents to denials")
    def activate(self, request, queryset):
        # updated_at moves too, so the matcher's corpus stamp changes and
        # denials are matched again (QuerySet.update skips auto_now).
        updated = queryset.update(is_active=True, updated_at=timezone.now())
        self.message_user(request, f"{updated} document(s) activated.")

    @admin.action(description="Deactivate: stop matching these documents")
    def deactivate(self, request, queryset):
        updated = queryset.update(is_active=False, updated_at=timezone.now())
        self.message_user(request, f"{updated} document(s) deactivated.")


@admin.register(PlanDocumentMatch)
class PlanDocumentMatchAdmin(admin.ModelAdmin):
    """Read-only: why a public plan document was, or was not, chosen for a
    denial (plan_document_matcher). Rows are written by the matcher only."""

    list_display = (
        "id",
        "denial_id",
        "document",
        "score",
        "rank",
        "selected",
        "method",
        "created_at",
    )
    list_select_related = ("document", "document__insurance_company")
    list_filter = ("selected", "method", "document__insurance_company")
    search_fields = ("denial__denial_id", "document__title", "document__document_key")
    raw_id_fields = ("denial",)
    ordering = ("-created_at", "rank")
    readonly_fields = (
        "denial",
        "document",
        "company",
        "score",
        "signals",
        "rank",
        "selected",
        "method",
        "relevant_text",
        "summary",
        "inputs_hash",
        "relevance_key",
        "created_at",
    )

    def get_queryset(self, request):
        # Never the documents' text, which runs to megabytes a row.
        return super().get_queryset(request).defer("document__text")

    def has_add_permission(self, request):  # type: ignore[override]
        return False

    def has_change_permission(self, request, obj=None):  # type: ignore[override]
        return False

    def has_delete_permission(self, request, obj=None):  # type: ignore[override]
        # The matcher owns these rows: deleting a selected one by hand would
        # leave its candidates with nothing selected.
        return False


@admin.register(MedicationContext)
class MedicationContextAdmin(admin.ModelAdmin):
    """Admin configuration for curated drug-class appeal context."""

    list_display = ("id", "drug_class", "active")
    list_filter = ("active",)
    search_fields = ("drug_class", "brand_names", "generic_names")
    ordering = ("drug_class",)
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "drug_class",
                    "active",
                    "brand_names",
                    "generic_names",
                    "regex",
                ),
            },
        ),
        (
            "Curated Appeal Content",
            {
                "fields": (
                    "fda_indications",
                    "common_denial_reasons",
                    "appeal_context",
                    "pubmed_ids",
                ),
                "description": (
                    "Injected verbatim into the appeal generation prompt "
                    "when the regex matches a denial. Keep concise and "
                    "factual; do not include unverified citations."
                ),
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
        "appeal_fax_number",
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
            "Appeal Routing (overrides company-level)",
            {
                "fields": (
                    "appeal_address",
                    "appeal_fax_number",
                    "appeal_phone_number",
                    "appeal_email",
                    "appeals_portal_url",
                    "appeals_info_url",
                ),
                "description": (
                    "Plan-specific appeal routing. Blank fields fall back "
                    "to the parent InsuranceCompany's defaults."
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


@admin.register(PayerPriorAuthRequirement)
class PayerPriorAuthRequirementAdmin(admin.ModelAdmin):
    """Admin configuration for indexed payer prior-auth requirement rules."""

    list_display = (
        "id",
        "insurance_company",
        "cpt_hcpcs_code",
        "code_range_start",
        "code_range_end",
        "line_of_business",
        "state",
        "requires_pa",
        "notification_only",
        "pa_category",
    )
    list_filter = (
        "insurance_company",
        "line_of_business",
        "state",
        "requires_pa",
        "notification_only",
        "pa_category",
    )
    search_fields = (
        "cpt_hcpcs_code",
        "code_description",
        "pa_category",
        "criteria_reference",
        "insurance_company__name",
        "notes",
    )
    autocomplete_fields = ["insurance_company", "plan"]
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "insurance_company",
                    "plan",
                    "line_of_business",
                    "state",
                ),
            },
        ),
        (
            "Code(s)",
            {
                "fields": (
                    "cpt_hcpcs_code",
                    "code_range_start",
                    "code_range_end",
                    "code_description",
                    "pa_category",
                ),
            },
        ),
        (
            "Rule",
            {
                "fields": (
                    "requires_pa",
                    "notification_only",
                    "submission_channel",
                    "criteria_reference",
                    "criteria_url",
                ),
            },
        ),
        (
            "Source / Validity",
            {
                "fields": (
                    "source_document",
                    "source_document_date",
                    "effective_date",
                    "end_date",
                    "notes",
                ),
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

    list_display = ("id", "name", "parent", "core")
    search_fields = ("name",)
    list_filter = ("parent", "core")
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
        "failure_reason",
        "denial_id",
    )
    search_fields = ("email", "name")
    list_filter = ("date", "paid", "fax_success", "sent", "failure_reason")
    ordering = ("-date",)
    raw_id_fields = ("denial_id", "for_appeal")
    list_select_related = ("denial_id",)


@admin.register(KnownNonFaxNumber)
class KnownNonFaxNumberAdmin(admin.ModelAdmin):
    """Numbers people fax appeals to that don't take faxes (mostly insurer
    voice lines). Carriers' appeal_phone_number values count too, without
    a row here; see fax_number_check."""

    list_display = ("number", "kind", "label", "insurance_company", "created")
    list_filter = ("kind",)
    search_fields = ("number", "label", "notes")
    ordering = ("number",)
    raw_id_fields = ("insurance_company",)


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

    list_display = (
        "id",
        "for_denial",
        "chosen",
        "editted",
        "context_level",
        "prompt_version",
        "speculative",
    )
    search_fields = ("appeal_text",)
    list_filter = (
        "chosen",
        "editted",
        "context_level",
        "prompt_version",
        "speculative",
    )
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
    """Admin configuration for OngoingChat model with chat editor UI."""

    change_form_template = "admin/fighthealthinsurance/ongoingchat/chat_editor.html"
    list_display = (
        "id",
        "professional_user",
        "user",
        "chat_type",
        "is_patient",
        "denied_item",
        "denied_reason",
        "message_count",
        "has_edited",
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
        "chat_type",
        "is_patient",
        "created_at",
        "updated_at",
        "domain",
    )
    ordering = ("-updated_at",)
    readonly_fields = ("id", "created_at", "updated_at", "chat_history")
    fieldsets = (
        (
            None,
            {
                "fields": ("id", "professional_user", "user", "is_patient", "domain"),
            },
        ),
        (
            "Denial Info",
            {
                "fields": ("denied_item", "denied_reason"),
            },
        ),
        (
            "Chat Data",
            {
                "fields": ("chat_history", "edited_chat_history"),
                "description": "Use the chat editor below the form to view and edit messages in a friendly interface.",
            },
        ),
        (
            "Metadata",
            {
                "fields": (
                    "session_key",
                    "hashed_email",
                    "microsite_slug",
                    "created_at",
                    "updated_at",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            "Usage Tracking",
            {
                "fields": ("user_agent", "asn", "asn_name", "ip_address"),
                "classes": ("collapse",),
            },
        ),
    )

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if connection.vendor == "postgresql":
            sql = (
                "CASE WHEN jsonb_typeof(chat_history) = 'array'"
                " THEN jsonb_array_length(chat_history) ELSE 0 END"
            )
        elif connection.vendor == "mysql":
            sql = (
                "CASE WHEN JSON_TYPE(chat_history) = 'ARRAY'"
                " THEN JSON_LENGTH(chat_history) ELSE 0 END"
            )
        else:
            sql = (
                "CASE WHEN json_type(chat_history) = 'array'"
                " THEN json_array_length(chat_history) ELSE 0 END"
            )
        return qs.annotate(
            chat_message_count=RawSQL(sql, [], output_field=IntegerField())
        )

    @admin.display(description="Messages", ordering="chat_message_count")
    def message_count(self, obj: OngoingChat) -> int:
        if hasattr(obj, "chat_message_count"):
            return int(obj.chat_message_count)
        if isinstance(obj.chat_history, list):
            return len(obj.chat_history)
        return 0

    @admin.display(boolean=True, description="Edited")
    def has_edited(self, obj: OngoingChat) -> bool:
        return (
            isinstance(obj.edited_chat_history, list)
            and len(obj.edited_chat_history) > 0
        )


@admin.register(ChatTurn)
class ChatTurnAdmin(admin.ModelAdmin):
    """Which models raced for each chat turn (read-only).

    Rows are written by the chat and hold metadata only. Deleting stays
    allowed: the admin refuses to delete an OngoingChat when a registered
    related model can't be deleted, so blocking it here would block
    deleting chats.
    """

    list_display = (
        "id",
        "chat_id",
        "created_at",
        "outcome",
        "winner_model",
        "winner_pass",
        "first_pass_model",
        "use_external",
        "alternate_offered",
        "alternate_model",
        "preferred",
    )
    search_fields = (
        "id",
        "chat__id",
        "winner_model",
        "first_pass_model",
        "alternate_model",
    )
    list_filter = (
        "outcome",
        "use_external",
        "alternate_offered",
        "alternate_cross_model",
        "preferred",
        "retry_used",
        "gate_used",
        "gate_outcome",
        "shadow_outcome",
        "created_at",
    )
    ordering = ("-created_at",)

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class HumanJudgeUploadedFilter(admin.SimpleListFilter):
    """Arena cases by whether their drafts have gone to HumanJudge."""

    title = "HumanJudge"
    parameter_name = "humanjudge"

    def lookups(self, request, model_admin):
        return (("sent", "Uploaded"), ("unsent", "Not uploaded"))

    def queryset(self, request, queryset):
        sent = HumanJudgeSubmission.objects.filter(status="SENT")
        if self.value() == "sent":
            return queryset.filter(id__in=sent.values("task_id"))
        if self.value() == "unsent":
            return queryset.exclude(id__in=sent.values("task_id"))
        return queryset


def _humanjudge_upload(model_admin, request, task_ids) -> None:
    """Send these Arena cases' drafts to HumanJudge and say how it went:
    while the page waits for a few cases, in the background for more
    (chooser_humanjudge.start_upload). Lazy import: the module pulls in the
    ML router."""
    from fighthealthinsurance import chooser_humanjudge

    ids = list(task_ids)
    if not ids:
        model_admin.message_user(
            request, "Nothing to upload: no case is waiting to go.", messages.INFO
        )
        return
    try:
        result = chooser_humanjudge.start_upload(ids)
    except chooser_humanjudge.HumanJudgeError as e:
        model_admin.message_user(request, f"Nothing uploaded: {e}", messages.ERROR)
        return
    if result is None:
        model_admin.message_user(
            request,
            f"Uploading {len(ids)} case(s) to HumanJudge in the background. "
            "HumanJudge Submissions shows each draft as it goes; any that fail "
            "can be sent again from there.",
            messages.SUCCESS,
        )
        return
    level = messages.WARNING if result.failed else messages.SUCCESS
    parts = [f"Uploaded {result.sent} draft(s) to HumanJudge."]
    if result.already_sent:
        parts.append(f"{result.already_sent} had already gone.")
    if result.failed:
        parts.append(
            f"{result.failed} failed (see HumanJudge Submissions; upload again to retry)."
        )
    if result.not_sendable:
        parts.append(
            f"{result.not_sendable} case(s) skipped: only synthetic appeal cases "
            "that have reached voters may go."
        )
    model_admin.message_user(request, " ".join(parts), level)


def _humanjudge_count(raw: Optional[str], default: int) -> Optional[int]:
    """The "how many cases" field: a whole number from 1 to the upload cap,
    or None for anything else."""
    from fighthealthinsurance.chooser_humanjudge import MAX_TASKS_PER_UPLOAD

    try:
        count = int(raw) if raw else default
    except ValueError:
        return None
    return count if 1 <= count <= MAX_TASKS_PER_UPLOAD else None


@admin.register(ChooserTask)
class ChooserTaskAdmin(admin.ModelAdmin):
    """Admin configuration for ChooserTask model."""

    list_display = (
        "id",
        "task_type",
        "status",
        "source",
        "case_version",
        "complexity",
        "settled_reason",
        "num_candidates_expected",
        "num_candidates_generated",
        "created_at",
    )
    search_fields = ("id", "source")
    list_filter = (
        "task_type",
        "status",
        "settled_reason",
        "case_version",
        "complexity",
        HumanJudgeUploadedFilter,
        "created_at",
    )
    ordering = ("-created_at",)
    readonly_fields = ("id", "created_at", "updated_at")
    actions = ("upload_to_humanjudge",)
    # The HumanJudge buttons: "Upload ... cases" on the list, "Upload to
    # HumanJudge" on a case (chooser_humanjudge).
    change_list_template = "admin/fighthealthinsurance/choosertask/change_list.html"
    change_form_template = "admin/fighthealthinsurance/choosertask/change_form.html"

    @admin.action(description="Upload to HumanJudge (synthetic appeal cases only)")
    def upload_to_humanjudge(self, request, queryset):
        _humanjudge_upload(self, request, queryset.values_list("id", flat=True))

    def get_urls(self):
        return [
            path(
                "humanjudge/upload-unsent/",
                self.admin_site.admin_view(self.humanjudge_upload_unsent_view),
                name="fighthealthinsurance_choosertask_humanjudge_upload_unsent",
            ),
            path(
                "<int:object_id>/humanjudge/upload/",
                self.admin_site.admin_view(self.humanjudge_upload_one_view),
                name="fighthealthinsurance_choosertask_humanjudge_upload",
            ),
        ] + super().get_urls()

    def changelist_view(self, request, extra_context=None):
        from fighthealthinsurance.chooser_humanjudge import (
            INLINE_UPLOAD_LIMIT,
            MAX_TASKS_PER_UPLOAD,
        )

        return super().changelist_view(
            request,
            {
                **(extra_context or {}),
                "humanjudge_default_count": INLINE_UPLOAD_LIMIT,
                "humanjudge_inline_limit": INLINE_UPLOAD_LIMIT,
                "humanjudge_max_count": MAX_TASKS_PER_UPLOAD,
            },
        )

    def change_view(self, request, object_id, form_url="", extra_context=None):
        from fighthealthinsurance.chooser_humanjudge import sendable_tasks

        context = dict(extra_context or {})
        if str(object_id).isdigit():
            drafts = ChooserCandidate.objects.filter(task_id=object_id, is_active=True)
            context.update(
                {
                    "humanjudge_sendable": sendable_tasks()
                    .filter(id=object_id)
                    .exists(),
                    "humanjudge_drafts": drafts.count(),
                    "humanjudge_sent": drafts.filter(
                        humanjudge_submission__status="SENT"
                    ).count(),
                }
            )
        return super().change_view(request, object_id, form_url, context)

    def humanjudge_upload_unsent_view(self, request):
        """POST: upload the next ``count`` cases waiting to go (undecided
        first, then newest; chooser_humanjudge.unsent_task_ids)."""
        from fighthealthinsurance.chooser_humanjudge import (
            INLINE_UPLOAD_LIMIT,
            MAX_TASKS_PER_UPLOAD,
            unsent_task_ids,
        )

        changelist = reverse("admin:fighthealthinsurance_choosertask_changelist")
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        if not self.has_change_permission(request):
            raise PermissionDenied
        count = _humanjudge_count(request.POST.get("count"), INLINE_UPLOAD_LIMIT)
        if count is None:
            self.message_user(
                request,
                f"Nothing uploaded: how many must be a whole number from 1 to "
                f"{MAX_TASKS_PER_UPLOAD}.",
                messages.ERROR,
            )
            return HttpResponseRedirect(changelist)
        _humanjudge_upload(self, request, unsent_task_ids(count))
        return HttpResponseRedirect(changelist)

    def humanjudge_upload_one_view(self, request, object_id):
        """POST: upload this case's drafts not sent yet."""
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        if not self.has_change_permission(request):
            raise PermissionDenied
        _humanjudge_upload(self, request, [object_id])
        return HttpResponseRedirect(
            reverse("admin:fighthealthinsurance_choosertask_change", args=[object_id])
        )


@admin.register(ChooserCandidate)
class ChooserCandidateAdmin(admin.ModelAdmin):
    """Admin configuration for ChooserCandidate model."""

    list_display = (
        "id",
        "task",
        "candidate_index",
        "kind",
        "model_name",
        "synthesized_by",
        "is_active",
        "created_at",
    )
    search_fields = ("task__id", "model_name", "synthesized_by", "content")
    list_filter = ("kind", "synthesized", "is_active", "created_at")
    ordering = ("-created_at",)
    readonly_fields = ("id", "created_at")


@admin.register(ChooserVote)
class ChooserVoteAdmin(admin.ModelAdmin):
    """Admin configuration for ChooserVote model."""

    list_display = (
        "id",
        "task",
        "chosen_candidate",
        "tied_candidate_ids",
        "all_bad",
        "seconds_to_vote",
        "active_ms",
        "expanded_candidate_ids",
        "network_class",
        "session_key",
        "created_at",
    )
    search_fields = ("task__id", "session_key", "rationale")
    list_filter = ("all_bad", "network_class", "created_at")
    ordering = ("-created_at",)
    readonly_fields = ("id", "created_at", "seconds_to_vote")


@admin.register(HumanJudgeSubmission)
class HumanJudgeSubmissionAdmin(admin.ModelAdmin):
    """Arena drafts sent to HumanJudge (see chooser_humanjudge)."""

    list_display = (
        "id",
        "task",
        "candidate",
        "status",
        "attempts",
        "last_error",
        "sent_at",
        "created_at",
    )
    search_fields = ("task__id", "inference_id")
    list_filter = ("status", "created_at")
    ordering = ("-created_at",)
    readonly_fields = ("id", "inference_id", "created_at", "sent_at")
    actions = ("upload_again",)

    def has_add_permission(self, request):
        # Made by "Upload to HumanJudge" on the Chooser Task list.
        return False

    @admin.action(description="Upload again (sends their cases' unsent drafts)")
    def upload_again(self, request, queryset):
        _humanjudge_upload(
            self, request, queryset.values_list("task_id", flat=True).distinct()
        )


@admin.register(HumanJudgeVerdict)
class HumanJudgeVerdictAdmin(admin.ModelAdmin):
    """HumanJudge's verdicts, kept apart from the Arena's own votes. The
    list's "Download verdicts from HumanJudge" button fetches new ones."""

    change_list_template = (
        "admin/fighthealthinsurance/humanjudgeverdict/change_list.html"
    )
    list_display = (
        "id",
        "task",
        "candidate",
        "verdict",
        "flag_category",
        "evaluator_id",
        "voted_at",
        "fetched_at",
    )
    search_fields = ("task__id", "evaluator_id", "feedback")
    list_filter = ("verdict", "flag_category", "voted_at")
    ordering = ("-fetched_at",)
    readonly_fields = ("id", "external_key", "raw", "fetched_at")
    actions = ("export_csv",)

    CSV_FIELDS = (
        "task_id",
        "candidate_id",
        "model_name",
        "synthesized_by",
        "verdict",
        "flag_category",
        "feedback",
        "evaluator_id",
        "voted_at",
    )

    def has_add_permission(self, request):
        # Verdicts come only from HumanJudge.
        return False

    def get_urls(self):
        return [
            path(
                "download/",
                self.admin_site.admin_view(self.download_view),
                name="fighthealthinsurance_humanjudgeverdict_download",
            ),
            path(
                "breakdown/",
                self.admin_site.admin_view(self.breakdown_view),
                name="fighthealthinsurance_humanjudgeverdict_breakdown",
            ),
        ] + super().get_urls()

    def breakdown_view(self, request):
        """HumanJudge's verdicts by model beside the Arena's own record
        (humanjudge_reports.breakdown); ``?format=json`` for the same data.
        admin_view sends anyone not signed in as staff to the admin login,
        and a staff member who may not view verdicts gets a 403, JSON
        included: it carries reviewers' written feedback."""
        if not self.has_view_permission(request):
            raise PermissionDenied
        from fighthealthinsurance.humanjudge_reports import breakdown

        data = breakdown()
        if request.GET.get("format") == "json":
            response = JsonResponse(data)
            response["Cache-Control"] = "private, no-store"
            return response
        return TemplateResponse(
            request,
            "admin/fighthealthinsurance/humanjudgeverdict/breakdown.html",
            {
                **self.admin_site.each_context(request),
                "title": "HumanJudge breakdown",
                "opts": self.model._meta,
                "data": data,
            },
        )

    def download_view(self, request):
        """POST only: fetch HumanJudge's new verdicts, then back to the list."""
        changelist = reverse("admin:fighthealthinsurance_humanjudgeverdict_changelist")
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        if not self.has_change_permission(request):
            raise PermissionDenied
        from asgiref.sync import async_to_sync

        from fighthealthinsurance import chooser_humanjudge

        try:
            result = async_to_sync(chooser_humanjudge.download_verdicts)()
        except chooser_humanjudge.HumanJudgeError as e:
            self.message_user(request, f"Nothing downloaded: {e}", messages.ERROR)
            return HttpResponseRedirect(changelist)
        message = (
            f"Downloaded {result.new_verdicts} new verdict(s) on {result.cases} "
            f"case(s); {result.picks} of those cases now have a HumanJudge pick."
        )
        if result.unmatched:
            message += (
                f" {result.unmatched} vote(s) matched no draft we uploaded and "
                "were left out."
            )
        self.message_user(request, message, messages.SUCCESS)
        return HttpResponseRedirect(changelist)

    @admin.action(description="Export selected verdicts as CSV")
    def export_csv(self, request, queryset):
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = (
            'attachment; filename="humanjudge_verdicts.csv"'
        )
        writer = csv.writer(response)
        writer.writerow(self.CSV_FIELDS)
        for verdict in queryset.select_related("candidate").order_by("task_id", "id"):
            writer.writerow(
                [
                    verdict.task_id,
                    verdict.candidate_id,
                    verdict.candidate.model_name,
                    verdict.candidate.synthesized_by or "",
                    verdict.verdict,
                    verdict.flag_category,
                    verdict.feedback,
                    verdict.evaluator_id,
                    verdict.voted_at.isoformat() if verdict.voted_at else "",
                ]
            )
        return response


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


class PlanSponsorForm(forms.ModelForm):
    """A company's name and EIN, refused here rather than at the database
    when the list already has that name with no EIN and this has none."""

    class Meta:
        model = PlanSponsor
        fields = ["name", "ein"]

    def clean_ein(self):
        # Stored as 12-3456789, the form documents give, so the two match;
        # blank is no EIN, so two companies without one don't clash on "".
        digits = "".join(c for c in (self.cleaned_data.get("ein") or "") if c.isdigit())
        typed = (self.cleaned_data.get("ein") or "").strip()
        if not typed:
            return None
        if len(digits) != 9 or any(c not in "0123456789- " for c in typed):
            raise forms.ValidationError("An EIN is nine digits, as 12-3456789.")
        return f"{digits[:2]}-{digits[2:]}"

    def clean(self):
        from fighthealthinsurance.plan_sponsors import normalize_name

        cleaned = super().clean() or {}
        normalized = normalize_name(cleaned.get("name") or "")
        if "name" in cleaned and not normalized:
            self.add_error("name", "Give the company's name.")
        elif normalized and not cleaned.get("ein"):
            same = PlanSponsor.objects.filter(
                normalized_name=normalized, ein__isnull=True
            ).exclude(pk=self.instance.pk)
            if same.exists():
                self.add_error(
                    "name",
                    f"{same.first()} is already on the list with no EIN. "
                    "Use that one, or give this one its EIN.",
                )
        return cleaned


@admin.register(PlanSponsor)
class PlanSponsorAdmin(admin.ModelAdmin):
    """The running list of companies behind shared plan documents. Staff fix
    a name or EIN here, or add a company to link a document to by hand."""

    form = PlanSponsorForm
    list_display = ("name", "ein", "documents", "created_at")
    search_fields = ("name", "normalized_name", "ein")
    readonly_fields = ("normalized_name", "first_document", "created_at")

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(document_count=Count("policy_documents"))
        )

    @admin.display(description="Documents", ordering="document_count")
    def documents(self, obj: PlanSponsor) -> int:
        return getattr(obj, "document_count", 0)

    def save_model(self, request, obj, form, change):
        from fighthealthinsurance.plan_sponsors import normalize_name

        obj.normalized_name = normalize_name(obj.name)
        super().save_model(request, obj, form, change)


class CompanyCheckFilter(admin.SimpleListFilter):
    """Shared documents by whether staff have checked the company the
    reading found. Optional: a company counts as soon as it's linked."""

    title = "company check"
    parameter_name = "company_check"

    def lookups(self, request, model_admin):
        return (
            ("unchecked", "Company found, not checked"),
            ("checked", "Company checked"),
            ("none", "No company"),
        )

    def queryset(self, request, queryset):
        if self.value() == "unchecked":
            return queryset.filter(
                plan_sponsor__isnull=False, plan_sponsor_checked_at__isnull=True
            )
        if self.value() == "checked":
            return queryset.filter(plan_sponsor_checked_at__isnull=False)
        if self.value() == "none":
            return queryset.filter(plan_sponsor__isnull=True)
        return queryset


@admin.register(PolicyDocument)
class PolicyDocumentAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "filename",
        "source",
        "document_type",
        "plan_category",
        "plan_sponsor",
        "plan_sponsor_source",
        "company_checked",
        "processing_status",
        "created_at",
        "file",
        "link",
    )
    list_filter = (
        "source",
        "processing_status",
        "unreadable_reason",
        CompanyCheckFilter,
        "plan_sponsor_source",
        "document_type",
        "plan_category",
        "created_at",
    )
    # A shared link is stored encrypted, so it can't be searched.
    search_fields = (
        "filename",
        "hashed_email",
        "insurer_name",
        "employer_name",
    )
    # Staff reach the file and its cleaned text through the links below,
    # never the raw fields; the cleaned text runs long. A shared link is
    # shown, never edited. The email a person gave on the share page is
    # shown on the row's own page (contact_email_shown), never in the list,
    # a search or a form that could write it back.
    exclude = ("document_enc", "cleaned_text", "shared_link", "contact_email")
    # The company is set by the reading; staff can fix it.
    autocomplete_fields = ("plan_sponsor",)
    readonly_fields = (
        "id",
        "created_at",
        "file",
        "link",
        "source",
        "shared_consent_version",
        "shared_at",
        "share_batch",
        "fingerprint",
        "shared_link_fingerprint",
        "processing_status",
        "processed_at",
        "unreadable_reason",
        "read_attempts",
        "read_attempted_at",
        "company_lookup_failures",
        "staff_says_plan",
        "plan_sponsor_source",
        "plan_sponsor_evidence",
        "plan_sponsor_checked_at",
        "plan_sponsor_checked_by",
        "insurer_name_source",
        "plan_year_source",
        "plan_category_source",
        "contact_email_shown",
    )
    actions = (
        "read_again",
        "mark_personal",
        "read_again_as_a_plan",
        "mark_potential_spam",
        "not_spam",
        "confirm_company",
    )

    @admin.display(description="email to write to (kept encrypted)")
    def contact_email_shown(self, obj):
        # Staff only, on this page alone: decrypted here for reading.
        return obj.contact_email or "-"

    @admin.display(boolean=True, description="checked")
    def company_checked(self, obj):
        if obj.plan_sponsor_id is None:
            return None
        return obj.plan_sponsor_checked_at is not None

    def save_model(self, request, obj, form, change):
        # A company staff set or changed is theirs, and checked.
        fields = list(form.changed_data)
        if "plan_sponsor" in fields:
            obj.plan_sponsor_source = "staff" if obj.plan_sponsor_id else ""
            obj.plan_sponsor_evidence = ""
            obj.plan_sponsor_checked_at = (
                timezone.now() if obj.plan_sponsor_id else None
            )
            obj.plan_sponsor_checked_by = request.user if obj.plan_sponsor_id else None
            fields += [
                "plan_sponsor_source",
                "plan_sponsor_evidence",
                "plan_sponsor_checked_at",
                "plan_sponsor_checked_by",
            ]
        # A fact staff set, changed or cleared is theirs: a reading never
        # fills it again (plan_documents_processing.EMPTY_PLAN_FACTS).
        for fact in ("insurer_name", "plan_year", "plan_category"):
            if fact in form.changed_data:
                setattr(obj, f"{fact}_source", "staff")
                fields.append(f"{fact}_source")
        # Only what the form changed: the encrypted fields it excludes are
        # never written back from what this request read.
        if change:
            obj.save(update_fields=fields)
            return
        super().save_model(request, obj, form, change)

    @admin.action(description="Confirm the company found")
    def confirm_company(self, request, queryset):
        # Optional: the company already counts. This records that a person
        # looked at it.
        updated = queryset.filter(plan_sponsor__isnull=False).update(
            plan_sponsor_checked_at=timezone.now(),
            plan_sponsor_checked_by=request.user,
        )
        self.message_user(
            request,
            f"{updated} compan{'y' if updated == 1 else 'ies'} confirmed. To fix "
            "one, open the document and pick the right company.",
        )

    @admin.action(description="Read again (shared files marked unreadable)")
    def read_again(self, request, queryset):
        # Back to waiting: the hourly sweep starts a new reading for them.
        updated = queryset.filter(
            source__in=PolicyDocument.SHARED_FILE_SOURCES,
            processing_status=PolicyDocument.PROCESSING_UNREADABLE,
        ).update(
            processing_status=PolicyDocument.PROCESSING_RECEIVED,
            processed_at=None,
            cleaned_text="",
            unreadable_reason="",
            read_attempts=0,
            read_attempted_at=None,
        )
        self.message_user(
            request,
            f"{updated} file(s) will be read again by the next hourly sweep; "
            f"{queryset.count() - updated} were not shared files marked unreadable.",
        )

    @admin.action(description="Personal papers: clear the text, keep the file")
    def mark_personal(self, request, queryset):
        # A shared file read as a plan, or as not a plan, that is a person's
        # own papers: its text goes, the encrypted file stays, and it is
        # never counted. Removed rows, spam and rows waiting are left alone.
        updated = queryset.filter(
            source__in=PolicyDocument.SHARED_FILE_SOURCES,
            processing_status__in=(
                PolicyDocument.PROCESSING_READY,
                PolicyDocument.PROCESSING_NOT_A_PLAN,
            ),
        ).update(
            processing_status=PolicyDocument.PROCESSING_PERSONAL,
            cleaned_text="",
            plan_sponsor_evidence="",
            staff_says_plan=False,
        )
        logger.info(
            f"Staff user {request.user.pk} marked {updated} policy document(s) "
            "as personal papers"
        )
        self.message_user(
            request,
            f"{updated} marked as personal papers: text cleared, file kept; "
            f"{queryset.count() - updated} were not shared files read as a plan "
            "or as not a plan.",
        )

    @admin.action(description="It is a plan: read again")
    def read_again_as_a_plan(self, request, queryset):
        # Back to waiting, marked as a plan by staff, so the next reading
        # stores its text as ready without the kind check.
        updated = queryset.filter(
            source__in=PolicyDocument.SHARED_FILE_SOURCES,
            processing_status__in=(
                PolicyDocument.PROCESSING_PERSONAL,
                PolicyDocument.PROCESSING_NOT_A_PLAN,
            ),
        ).update(
            processing_status=PolicyDocument.PROCESSING_RECEIVED,
            processed_at=None,
            cleaned_text="",
            unreadable_reason="",
            read_attempts=0,
            read_attempted_at=None,
            staff_says_plan=True,
        )
        logger.info(
            f"Staff user {request.user.pk} marked {updated} policy document(s) "
            "as plans to read again"
        )
        self.message_user(
            request,
            f"{updated} file(s) will be read again as plans by the next hourly "
            f"sweep; {queryset.count() - updated} were not shared files read "
            "as personal papers or as not a plan.",
        )

    @admin.action(description="Mark as potential spam")
    def mark_potential_spam(self, request, queryset):
        # Kept with its file, never read again and left out of every count.
        # Rows already removed at the person's request stay removed.
        rows = queryset.exclude(
            processing_status__in=(
                PolicyDocument.PROCESSING_SPAM,
                PolicyDocument.PROCESSING_REMOVED,
            )
        )
        updated = rows.update(processing_status=PolicyDocument.PROCESSING_SPAM)
        logger.info(
            f"Staff user {request.user.pk} marked {updated} policy document(s) "
            "as potential spam"
        )
        self.message_user(
            request,
            f"{updated} marked as potential spam; they stay out of every count "
            'and are never read. Use "Not spam: read again" to undo.',
        )

    @admin.action(description="Not spam: read again")
    def not_spam(self, request, queryset):
        # Back to where it would be had it never been marked: a shared file
        # waits for the hourly sweep to read it again, a shared link is a
        # link again, and any other upload has no reading status.
        # One transaction, and each kind of row in its own update: the last
        # update never reaches a shared file or link, so one marked again
        # meanwhile keeps its status instead of losing it.
        spam = queryset.filter(processing_status=PolicyDocument.PROCESSING_SPAM)
        with transaction.atomic():
            files = spam.filter(source__in=PolicyDocument.SHARED_FILE_SOURCES).update(
                processing_status=PolicyDocument.PROCESSING_RECEIVED,
                processed_at=None,
                cleaned_text="",
                unreadable_reason="",
                read_attempts=0,
                read_attempted_at=None,
            )
            links = spam.filter(source=PolicyDocument.SOURCE_SHARED_LINK).update(
                processing_status=PolicyDocument.PROCESSING_LINK
            )
            others = (
                spam.exclude(source__in=PolicyDocument.SHARED_FILE_SOURCES)
                .exclude(source=PolicyDocument.SOURCE_SHARED_LINK)
                .update(processing_status="")
            )
        updated = files + links + others
        logger.info(
            f"Staff user {request.user.pk} marked {updated} policy document(s) "
            "as not spam"
        )
        self.message_user(
            request,
            f"{updated} no longer marked as potential spam; {files} shared "
            "file(s) will be read again by the next hourly sweep.",
        )

    def changelist_view(self, request, extra_context=None):
        # How many shared files there are, how many different documents, and
        # how many shared links; then the plans they make up, by week, kind,
        # year and premium answer.
        from fighthealthinsurance.shared_plan_documents import share_counts

        # Potential spam stays out of these totals, as out of every count.
        shared = PolicyDocument.objects.filter(
            source__in=PolicyDocument.SHARED_FILE_SOURCES
        ).exclude(processing_status=PolicyDocument.PROCESSING_SPAM)
        extra_context = {
            **(extra_context or {}),
            "share_counts": share_counts(timezone.localdate()),
            "shared_files": shared.count(),
            "shared_documents": shared.exclude(fingerprint="")
            .values("fingerprint")
            .distinct()
            .count(),
            "shared_links": PolicyDocument.objects.filter(
                source=PolicyDocument.SOURCE_SHARED_LINK
            )
            .exclude(processing_status=PolicyDocument.PROCESSING_SPAM)
            .exclude(shared_link="")
            .count(),
        }
        return super().changelist_view(request, extra_context=extra_context)

    @admin.display(description="File")
    def file(self, obj: PolicyDocument) -> str:
        # Text first: reading it opens nothing a stranger made.
        if (
            not obj.pk
            or not obj.document_enc
            or obj.source not in PolicyDocument.SHARED_FILE_SOURCES
        ):
            return "-"
        links = format_html(
            '<a href="{}">Read as text</a> or <a href="{}">download the original</a>',
            reverse("staff_policy_document_text", kwargs={"pk": obj.pk}),
            reverse("staff_policy_document_download", kwargs={"pk": obj.pk}),
        )
        if obj.processing_status == PolicyDocument.PROCESSING_READY:
            # The cleaned text first: it has personal details taken out.
            links = format_html(
                '<a href="{}">Read the cleaned text</a>, {}',
                reverse("staff_policy_document_cleaned_text", kwargs={"pk": obj.pk}),
                links,
            )
        return links

    @admin.display(description="Link")
    def link(self, obj: PolicyDocument) -> str:
        # The address as text, then a link staff open by hand in a new tab.
        # The server never fetches it.
        url = obj.shared_link
        if not url:
            return "-"
        text = format_html('<span style="overflow-wrap: anywhere">{}</span>', url)
        if urlsplit(url).scheme.lower() not in ("http", "https"):
            return text
        return format_html(
            '{} <a href="{}" target="_blank" rel="noopener noreferrer">'
            "Open in a new tab</a>",
            text,
            url,
        )


@admin.register(PolicyDocumentAnalysis)
class PolicyDocumentAnalysisAdmin(admin.ModelAdmin):
    list_display = ("id", "policy_document", "user_question", "created_at")
    list_filter = ("created_at",)
    search_fields = ("user_question", "summary")
    readonly_fields = ("id", "created_at")


class SiteBannerAdminForm(forms.ModelForm):
    """SiteBanner form with a pre-canned message picker.

    ``canned_message`` is a non-model dropdown of ready-made notices (from
    ``constants.SITE_BANNER_CANNED_MESSAGES``); selecting one fills the message
    box client-side (see ``js/admin_site_banner.js``) so staff can start from a
    template during an incident and then edit it. The field itself is not saved.
    """

    canned_message = forms.ChoiceField(
        required=False,
        label="Pre-canned message",
        help_text=(
            "Optional: pick a ready-made message to fill the box below, then "
            "edit if needed. This dropdown itself is not saved."
        ),
        choices=[("", "--- Start from a template (optional) ---")]
        + [(text, label) for label, text in SITE_BANNER_CANNED_MESSAGES],
    )

    class Meta:
        model = SiteBanner
        fields = "__all__"

    @property
    def media(self) -> forms.Media:
        # Versioned like the templates' scripts, so a CDN can't serve an old copy.
        return super().media + forms.Media(js=[versioned("js/admin_site_banner.js")])


@admin.register(SiteBanner)
class SiteBannerAdmin(admin.ModelAdmin):
    """Admin for the staff-controlled site header banner (no deploy needed)."""

    form = SiteBannerAdminForm
    list_display = (
        "id",
        "short_message",
        "level",
        "active",
        "dismissible",
        "expires_at",
        "updated_at",
    )
    list_display_links = ("id", "short_message")
    list_editable = ("active",)
    list_filter = ("active", "level", "dismissible")
    search_fields = ("message",)
    ordering = ("-updated_at",)
    readonly_fields = ("created_at", "updated_at")
    fieldsets = (
        (
            None,
            {
                "fields": ("canned_message", "message", "level"),
                "description": (
                    "Show a temporary notice in the site header to every "
                    "visitor. Pick a pre-canned message to start, then edit "
                    "as needed."
                ),
            },
        ),
        (
            "Visibility",
            {
                "fields": ("active", "dismissible", "expires_at"),
                "description": (
                    "Uncheck <strong>active</strong> (or set an expiry) to "
                    "remove the banner. Changes go live within ~30 seconds."
                ),
            },
        ),
        (
            "Timestamps",
            {
                "fields": ("created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )

    @admin.display(description="Message")
    def short_message(self, obj: SiteBanner) -> str:
        text = (obj.message or "").strip().replace("\n", " ")
        return (text[:80] + "…") if len(text) > 80 else text


@admin.register(PaidReviewBounty)
class PaidReviewBountyAdmin(admin.ModelAdmin):
    """Paid Arena reviewer batches posted to RentAHuman (paid_reviews.py).
    Set status to closed to stop the join link taking new workers."""

    list_display = (
        "id",
        "title",
        "status",
        "rah_bounty_id",
        "price_dollars",
        "spots",
        "reviews_required",
        "created_at",
    )
    list_filter = ("status",)
    readonly_fields = ("join_token", "created_at")


@admin.register(PaidReviewSession)
class PaidReviewSessionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "bounty",
        "status",
        "completion_code",
        "rah_submission_id",
        "code_issued_at",
        "claimed_at",
    )
    list_filter = ("status", "bounty")
    search_fields = ("completion_code", "rah_submission_id")
    readonly_fields = ("session_key", "created_at")
