import hashlib
import os
import re
import sys
import tempfile
import uuid
import typing
from loguru import logger

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Now
from django_prometheus.models import ExportModelOperationsMixin
from django_encrypted_filefield.fields import EncryptedFileField
from django.contrib.auth import get_user_model
from django_encrypted_filefield.crypt import Cryptographer

from fighthealthinsurance.utils import sekret_gen
from fhi_users.models import *
from regex_field.fields import RegexField

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


class GenericQuestionGeneration(ExportModelOperationsMixin("GenericQuestionGeneration"), models.Model):  # type: ignore
    """
    Stores cached question generations for specific procedure and diagnosis combinations
    that don't contain patient-specific information.
    """

    id = models.AutoField(primary_key=True)
    procedure = models.CharField(max_length=300)
    diagnosis = models.CharField(max_length=300)
    # Store the questions as a JSONField
    generated_questions = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["procedure", "diagnosis"]),
        ]

    def __str__(self):
        return f"Generic Questions: {self.procedure} / {self.diagnosis}"


class GenericContextGeneration(ExportModelOperationsMixin("GenericContextGeneration"), models.Model):  # type: ignore
    """
    Stores cached citation/context generations for specific procedure and diagnosis combinations
    that don't contain patient-specific information.
    """

    id = models.AutoField(primary_key=True)
    procedure = models.CharField(max_length=300)
    diagnosis = models.CharField(max_length=300)
    # Store the citations as a JSONField
    generated_context = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["procedure", "diagnosis"]),
        ]

    def __str__(self):
        return f"Generic Context: {self.procedure} / {self.diagnosis}"


# Money related :p
class InterestedProfessional(ExportModelOperationsMixin("InterestedProfessional"), models.Model):  # type: ignore
    """
    Stores information about professionals interested in using the service.
    Used for tracking professional signups and their payment status.
    """

    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=300, default="", blank=True)
    business_name = models.CharField(max_length=300, default="", blank=True)
    phone_number = models.CharField(max_length=300, default="", blank=True)
    address = models.CharField(max_length=1000, default="", blank=True)
    email = models.EmailField()
    comments = models.TextField(default="", blank=True)
    most_common_denial = models.CharField(max_length=300, default="", blank=True)
    job_title_or_provider_type = models.CharField(
        max_length=300, default="", blank=True
    )
    paid = models.BooleanField(default=False)
    clicked_for_paid = models.BooleanField(default=True)
    # Note: Was initially auto_now so data is kind of junk-ish prior to Feb 3rd
    signup_date = models.DateField(auto_now_add=True)
    mod_date = models.DateField(auto_now=True)
    thankyou_email_sent = models.BooleanField(default=False)


# Everyone else:
class MailingListSubscriber(models.Model):
    """
    Stores mailing list subscribers for newsletters and updates.
    Includes unsubscribe token for privacy-compliant unsubscription.
    """

    id = models.AutoField(primary_key=True)
    email = models.EmailField()
    name = models.CharField(max_length=300, default="", blank=True)
    comments = models.TextField(default="", blank=True)
    signup_date = models.DateField(auto_now_add=True)
    phone = models.CharField(max_length=300, default="", blank=True)
    unsubscribe_token = models.CharField(
        max_length=100, default=sekret_gen, unique=False
    )
    referral_source = models.CharField(
        max_length=300, default="", blank=True, null=True
    )
    referral_source_details = models.TextField(default="", blank=True, null=True)

    def __str__(self):
        return self.email

    def get_unsubscribe_url(self) -> str:
        """Generate the unsubscribe URL for this subscriber."""
        from django.urls import reverse

        return "https://www.fighthealthinsurance.com" + reverse(
            "unsubscribe", kwargs={"token": self.unsubscribe_token}
        )


class DemoRequests(models.Model):
    """
    Stores demo requests from potential enterprise customers.
    Tracks contact information and request source for sales follow-up.
    """

    id = models.AutoField(primary_key=True)
    email = models.EmailField()
    name = models.CharField(max_length=300, default="", blank=True)
    company = models.CharField(max_length=300, default="", blank=True)
    role = models.CharField(max_length=300, default="", blank=True)
    source = models.CharField(max_length=300, default="", blank=True)
    signup_date = models.DateField(auto_now_add=True)
    phone = models.CharField(max_length=300, default="", blank=True)

    def __str__(self):
        return self.email


class FollowUpType(models.Model):
    """
    Defines types of follow-up emails with their templates and timing.
    Used to schedule automated follow-up communications with users.
    """

    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=300, default="")
    subject = models.CharField(max_length=300, primary_key=False)
    text = models.TextField(max_length=30000, primary_key=False)
    duration = models.DurationField()

    def __str__(self):
        return self.name


class FollowUp(models.Model):
    """
    Stores user responses to follow-up emails about their appeals.
    Captures appeal outcomes, user feedback, and testimonials.
    """

    followup_result_id = models.AutoField(primary_key=True)
    hashed_email = models.CharField(max_length=200, null=True)
    denial_id = models.ForeignKey("Denial", on_delete=models.CASCADE)
    more_follow_up_requested = models.BooleanField(default=False)
    follow_up_medicare_someone_to_help = models.BooleanField(default=False)
    user_comments = models.TextField(null=True)
    quote = models.TextField(null=True)
    use_quote = models.BooleanField(default=False)
    email = models.CharField(max_length=300, null=True)
    name_for_quote = models.TextField(null=True)
    appeal_result = models.CharField(max_length=200, null=True)
    response_date = models.DateField(auto_now=False, auto_now_add=True)


class FollowUpSched(models.Model):
    """
    Schedules follow-up emails to be sent to users after their denial submission.
    Tracks sending status and handles cascade deletion when denials are removed.
    """

    follow_up_id = models.AutoField(primary_key=True)
    email = models.CharField(max_length=300, primary_key=False)
    follow_up_type = models.ForeignKey(
        FollowUpType, null=True, on_delete=models.SET_NULL
    )
    initial = models.DateField(auto_now=False, auto_now_add=True)
    follow_up_date = models.DateField(auto_now=False, auto_now_add=False)
    follow_up_sent = models.BooleanField(default=False)
    follow_up_sent_date = models.DateTimeField(null=True)
    attempting_to_send_as_of = models.DateField(
        auto_now=False, auto_now_add=False, null=True
    )
    # If the denial is deleted it's either SPAM or a PII removal request
    # in either case lets delete the scheduled follow ups.
    denial_id = models.ForeignKey("Denial", on_delete=models.CASCADE)

    def __str__(self):
        return f"{self.email} on {self.follow_up_date}"


class PlanType(models.Model):
    """
    Categorizes insurance plan types (e.g., HMO, PPO, Medicare).
    Uses regex patterns to automatically classify plans from denial text.
    """

    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=300, primary_key=False)
    alt_name = models.CharField(max_length=300, blank=True)
    regex = RegexField(
        max_length=400, re_flags=re.IGNORECASE | re.UNICODE | re.M, blank=True
    )
    negative_regex = RegexField(
        max_length=400, re_flags=re.IGNORECASE | re.UNICODE | re.M, blank=True
    )

    def __str__(self):
        return self.name


class Regulator(models.Model):
    """
    Stores information about insurance regulators (e.g., state insurance departments).
    Used to provide users with contact information for filing complaints.
    """

    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=300, primary_key=False)
    website = models.CharField(max_length=300, primary_key=False)
    alt_name = models.CharField(max_length=300, primary_key=False)
    regex = RegexField(max_length=400, re_flags=re.IGNORECASE | re.UNICODE | re.M)
    negative_regex = RegexField(
        max_length=400, re_flags=re.IGNORECASE | re.UNICODE | re.M
    )

    def __str__(self):
        return self.name


class PlanSource(models.Model):
    """
    Identifies the source/origin of insurance plans (e.g., employer, marketplace, Medicaid).
    Uses regex patterns to classify plan sources from denial text.
    """

    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=300, primary_key=False)
    regex = RegexField(max_length=400, re_flags=re.IGNORECASE | re.UNICODE | re.M)
    negative_regex = RegexField(
        max_length=400, re_flags=re.IGNORECASE | re.UNICODE | re.M
    )

    def __str__(self):
        return self.name


class Diagnosis(models.Model):
    """
    These represent rules for extracting a diagnosis from text.
    So called 'expert system' which is just a collection of regular
    expressions. We also use the ML models, but these are cheap to evaluate.
    """

    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=300, primary_key=False)
    regex = RegexField(max_length=400, re_flags=re.IGNORECASE | re.UNICODE | re.M)

    def __str__(self):
        return f"{self.id}:{self.name}"


class Procedures(models.Model):
    """
    Similar to diagnosis, but for procedures.
    """

    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=300, primary_key=False)
    regex = RegexField(max_length=400, re_flags=re.IGNORECASE | re.UNICODE | re.M)

    def __str__(self):
        return f"{self.id}:{self.name}"


class DenialTypes(models.Model):
    """
    Categorizes types of insurance denials (e.g., medical necessity, prior auth).
    Supports hierarchical categorization with parent types and custom appeal text.
    Uses regex patterns for automatic classification from denial letters.
    """

    id = models.AutoField(primary_key=True)
    # for the many different sub-variants.
    parent = models.ForeignKey(
        "self",
        blank=True,
        null=True,
        related_name="children",
        on_delete=models.SET_NULL,
    )
    name = models.CharField(max_length=300, primary_key=False)
    regex = RegexField(max_length=400, re_flags=re.IGNORECASE | re.UNICODE | re.M)
    diagnosis_regex = RegexField(
        max_length=400,
        re_flags=re.IGNORECASE | re.UNICODE | re.M,
        null=True,
        blank=True,
    )
    procedure_regex = RegexField(
        max_length=400,
        re_flags=re.IGNORECASE | re.UNICODE | re.M,
        null=True,
        blank=True,
    )
    negative_regex = RegexField(
        max_length=400, re_flags=re.IGNORECASE | re.UNICODE | re.M, blank=True
    )
    appeal_text = models.TextField(max_length=3000, blank=True)
    form = models.CharField(max_length=300, null=True, blank=True)

    def get_form(self):
        if self.form is None:
            parent = self.parent
            if parent is not None:
                return parent.get_form()
            else:
                return None
        else:
            import fighthealthinsurance.forms.questions

            try:
                return getattr(
                    sys.modules["fighthealthinsurance.forms.questions"], self.form
                )
            except Exception as e:
                logger.opt(exception=True).debug(f"Error loading form {self.form}: {e}")
                return None

    def __str__(self):
        return self.name


class AppealTemplates(models.Model):
    """
    Stores appeal letter templates for specific denial/diagnosis combinations.
    Templates are matched using regex patterns and included in generated appeals.
    """

    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=300, primary_key=False)
    regex = RegexField(max_length=400, re_flags=re.IGNORECASE | re.UNICODE | re.M)
    negative_regex = RegexField(
        max_length=400, re_flags=re.IGNORECASE | re.UNICODE | re.M
    )
    diagnosis_regex = RegexField(
        max_length=400, re_flags=re.IGNORECASE | re.UNICODE | re.M, blank=True
    )
    appeal_text = models.TextField(max_length=3000, blank=True)

    def __str__(self):
        return f"{self.id}:{self.name}"


class DataSource(models.Model):
    """
    Tracks the origin of data entries (e.g., user input, ML model, expert system).
    Used for auditing and understanding how information was classified.
    """

    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=100)

    def __str__(self):
        return f"{self.id}:{self.name}"


class PlanDocuments(models.Model):
    """
    Stores uploaded insurance plan documents associated with denials.
    Supports both encrypted and unencrypted file storage.
    """

    plan_document_id = models.AutoField(primary_key=True)
    plan_document = models.FileField(null=True, storage=settings.COMBINED_STORAGE)
    plan_document_enc = EncryptedFileField(null=True, storage=settings.COMBINED_STORAGE)
    # If the denial is deleted it's either SPAM or a removal request in either case
    # we cascade the delete
    denial = models.ForeignKey("Denial", on_delete=models.CASCADE)


class FollowUpDocuments(models.Model):
    """
    Stores documents uploaded during follow-up responses (e.g., appeal outcome letters).
    Supports both encrypted and unencrypted file storage.
    """

    document_id = models.AutoField(primary_key=True)
    follow_up_document = models.FileField(null=True, storage=settings.COMBINED_STORAGE)
    follow_up_document_enc = EncryptedFileField(
        null=True, storage=settings.COMBINED_STORAGE
    )
    # If the denial is deleted it's either SPAM or a removal request in either case
    # we cascade the delete
    denial = models.ForeignKey("Denial", on_delete=models.CASCADE)
    follow_up_id = models.ForeignKey("FollowUp", on_delete=models.CASCADE, null=True)


class PubMedArticleSummarized(models.Model):
    """PubMedArticles with a summary for the given query."""

    pmid = models.TextField(blank=True)
    doi = models.TextField(blank=True, null=True)
    query = models.TextField(blank=True)
    title = models.TextField(blank=True, null=True)
    abstract = models.TextField(blank=True, null=True)
    text = models.TextField(blank=True, null=True)
    basic_summary = models.TextField(blank=True, null=True)
    says_effective = models.BooleanField(null=True, blank=True)
    publication_date = models.DateTimeField(null=True, blank=True)
    retrival_date = models.TextField(blank=True, null=True)
    article_url = models.TextField(blank=True, null=True)


class PubMedMiniArticle(models.Model):
    """PubMedArticles with a summary for the given query."""

    pmid = models.TextField(blank=True)
    title = models.TextField(blank=True, null=True)
    abstract = models.TextField(blank=True, null=True)
    created = models.DateTimeField(db_default=Now(), null=True)
    article_url = models.TextField(blank=True, null=True)

    def __str__(self):
        return f"{self.pmid} -- {self.title}"


class PubMedQueryData(models.Model):
    """
    Caches PubMed search results for medical literature queries.
    Used to avoid redundant API calls and speed up appeal generation.
    """

    internal_id = models.AutoField(primary_key=True)
    query = models.TextField(null=False, max_length=300)
    since = models.TextField(null=True)  # text date for the since query
    articles = models.TextField(null=True)  # json
    query_date = models.DateTimeField(auto_now_add=True)
    denial_id = models.ForeignKey("Denial", on_delete=models.SET_NULL, null=True)
    created = models.DateTimeField(db_default=Now(), null=True)


class FaxesToSend(ExportModelOperationsMixin("FaxesToSend"), models.Model):  # type: ignore
    """
    Queues faxes to be sent with appeal documents.
    Tracks payment status, sending attempts, and success/failure.
    Supports both encrypted and unencrypted document storage.
    """

    fax_id = models.AutoField(primary_key=True)
    hashed_email = models.CharField(max_length=300, primary_key=False)
    date = models.DateTimeField(auto_now=False, auto_now_add=True)
    paid = models.BooleanField()
    email = models.CharField(max_length=300)
    name = models.CharField(max_length=300, null=True, blank=True)
    appeal_text = models.TextField()
    pmids = models.JSONField(null=True, blank=True)
    health_history = models.TextField(null=True, blank=True)
    combined_document = models.FileField(
        null=True, storage=settings.COMBINED_STORAGE, blank=True
    )
    combined_document_enc = EncryptedFileField(
        null=True, storage=settings.COMBINED_STORAGE, blank=True
    )
    uuid = models.CharField(max_length=300, default=uuid.uuid4, editable=False)
    sent = models.BooleanField(default=False)
    attempting_to_send_as_of = models.DateTimeField(
        auto_now=False, auto_now_add=False, null=True, blank=True
    )
    fax_success = models.BooleanField(default=False)
    denial_id = models.ForeignKey("Denial", on_delete=models.CASCADE, null=True)
    destination = models.CharField(max_length=20, null=True)
    should_send = models.BooleanField(default=False)
    # Professional we may use different backends.
    professional = models.BooleanField(default=False)
    for_appeal = models.ForeignKey(
        "Appeal", on_delete=models.SET_NULL, null=True, blank=True
    )

    def _get_contents(self):
        if self.combined_document:
            return self.combined_document.read()
        elif self.combined_document_enc:
            cryptographer = Cryptographer(settings.COMBINED_STORAGE)
            try:
                return cryptographer.decrypt(self.combined_document_enc.read())
            except:
                logger.opt(exception=True).debug(
                    f"Error reading encrypted document, sometimes this mean it was not encrypted falling back"
                )
                self.combined_document_enc.read()
        else:
            raise Exception("No file found (encrypted or unencrypted)")

    def _get_filename(self) -> str:
        if self.combined_document:
            return self.combined_document.name  # type: ignore
        elif self.combined_document_enc:
            return self.combined_document_enc.name  # type: ignore
        else:
            raise Exception("No file found (encrypted or unencrypted)")

    def get_temporary_document_path(self):
        with tempfile.NamedTemporaryFile(
            suffix=self._get_filename(), mode="w+b", delete=False
        ) as f:
            f.write(self._get_contents())
            f.flush()
            f.close()
            os.sync()
            return f.name

    def __str__(self):
        return f"{self.fax_id} -- {self.email} -- {self.paid} -- {self.fax_success} -- {self.name}"


class DenialTypesRelation(models.Model):
    """Many-to-many through table linking denials to their denial types with source tracking."""

    denial = models.ForeignKey("Denial", on_delete=models.CASCADE)
    denial_type = models.ForeignKey(DenialTypes, on_delete=models.CASCADE)
    src = models.ForeignKey(DataSource, on_delete=models.SET_NULL, null=True)


class PlanTypesRelation(models.Model):
    """Many-to-many through table linking denials to their plan types with source tracking."""

    denial = models.ForeignKey("Denial", on_delete=models.CASCADE)
    plan_type = models.ForeignKey(PlanType, on_delete=models.CASCADE)
    src = models.ForeignKey(DataSource, on_delete=models.SET_NULL, null=True)


class PlanSourceRelation(models.Model):
    """Many-to-many through table linking denials to their plan sources with source tracking."""

    denial = models.ForeignKey("Denial", on_delete=models.CASCADE)
    plan_source = models.ForeignKey(PlanSource, on_delete=models.CASCADE)
    src = models.ForeignKey(DataSource, on_delete=models.SET_NULL, null=True)


class Denial(ExportModelOperationsMixin("Denial"), models.Model):  # type: ignore
    """
    Core model representing an insurance denial submitted by a user.
    Stores denial text, extracted entities (procedure, diagnosis), user information,
    and links to generated appeals, PubMed references, and professional associations.
    Supports access control filtering for patients, professionals, and domain admins.
    """

    denial_id = models.AutoField(primary_key=True, null=False)
    uuid = models.CharField(max_length=300, default=uuid.uuid4, editable=False)
    hashed_email = models.CharField(max_length=300, primary_key=False)
    denial_text = models.TextField(primary_key=False)
    date_of_service_text = models.TextField(primary_key=False, null=True, blank=True)
    denial_type_text = models.TextField(max_length=200, null=True, blank=True)
    date = models.DateField(auto_now=False, auto_now_add=True)
    denial_type = models.ManyToManyField(DenialTypes, through=DenialTypesRelation)
    plan_type = models.ManyToManyField(PlanType, through=PlanTypesRelation)
    plan_source = models.ManyToManyField(PlanSource, through=PlanSourceRelation)
    employer_name = models.CharField(max_length=300, null=True, blank=True)
    regulator = models.ForeignKey(
        Regulator, null=True, on_delete=models.SET_NULL, blank=True
    )
    urgent = models.BooleanField(default=False)
    pre_service = models.BooleanField(default=False)
    denial_date = models.DateField(auto_now=False, null=True, blank=True)
    insurance_company = models.CharField(max_length=300, null=True, blank=True)
    claim_id = models.CharField(max_length=300, null=True, blank=True)
    procedure = models.CharField(max_length=300, null=True, blank=True)
    diagnosis = models.CharField(max_length=300, null=True, blank=True)
    # Keep track of if the async thread finished extracting procedure and diagnosis
    extract_procedure_diagnosis_finished = models.BooleanField(
        default=False, null=True, blank=True
    )
    appeal_text = models.TextField(null=True, blank=True)
    raw_email = models.TextField(max_length=300, null=True, blank=True)
    created = models.DateTimeField(db_default=Now(), null=True)
    use_external = models.BooleanField(default=False)
    health_history = models.TextField(null=True, blank=True)
    qa_context = models.TextField(null=True, blank=True)
    plan_context = models.TextField(null=True, blank=True)
    semi_sekret = models.CharField(max_length=100, default=sekret_gen)
    plan_id = models.CharField(max_length=200, null=True, blank=True)
    state = models.CharField(max_length=4, null=True, blank=True)
    appeal_result = models.CharField(max_length=200, null=True, blank=True)
    last_interaction = models.DateTimeField(auto_now=True)
    follow_up_semi_sekret = models.CharField(max_length=100, default=sekret_gen)
    references = models.TextField(null=True, blank=True)
    reference_summary = models.TextField(null=True, blank=True)
    appeal_fax_number = models.CharField(max_length=40, null=True, blank=True)
    your_state = models.CharField(max_length=40, null=True, blank=True)
    creating_professional = models.ForeignKey(
        ProfessionalUser,
        null=True,
        on_delete=models.SET_NULL,
        related_name="denials_created",
        blank=True,
    )
    primary_professional = models.ForeignKey(
        ProfessionalUser,
        null=True,
        on_delete=models.SET_NULL,
        related_name="denials_primary",
        blank=True,
    )
    patient_user = models.ForeignKey(
        PatientUser, null=True, on_delete=models.SET_NULL, blank=True
    )
    domain = models.ForeignKey(
        UserDomain, null=True, on_delete=models.SET_NULL, blank=True
    )
    patient_visible = models.BooleanField(default=True)
    # If the professional is the one submitting the appeal
    professional_to_finish = models.BooleanField(default=False)
    # Date of service can be many things which are not a simple date.
    date_of_service = models.CharField(
        null=True, max_length=300, default="", blank=True
    )
    # Provider in network
    provider_in_network = models.BooleanField(default=False, null=True)
    health_history_anonymized = models.BooleanField(default=True)
    single_case = models.BooleanField(default=False, null=True)
    # pubmed articles to be used to create the input context to the appeal
    pubmed_ids_json = models.JSONField(null=True, blank=True)
    pubmed_context = models.TextField(null=True, blank=True)
    generated_questions = models.JSONField(null=True, blank=True)
    # ML-generated citations for the appeal
    ml_citation_context = models.JSONField(null=True, blank=True)
    # ML-generated summary of relevant plan document sections
    plan_documents_summary = models.TextField(null=True, blank=True)
    manual_deidentified_denial = models.TextField(
        primary_key=False, null=True, default=""
    )
    manual_deidentified_ocr_cleaned_denial = models.TextField(
        primary_key=False, null=True, default=""
    )
    manual_deidentified_appeal = models.TextField(
        primary_key=False, null=True, default=""
    )
    manual_searchterm = models.TextField(primary_key=False, null=True, default="")
    verified_procedure = models.TextField(primary_key=False, null=True, default="")
    verified_diagnosis = models.TextField(primary_key=False, null=True, default="")
    flag_for_exclude = models.BooleanField(default=False, null=True)
    include_provided_health_history_in_appeal = models.BooleanField(default=False)
    # Used to mark claims related to dental services
    dental_claim = models.BooleanField(default=False)
    # Used to mark claims not related to human patients (e.g., pet insurance)
    not_human_claim = models.BooleanField(default=False)
    # Marks this denial as a unique claim example for training or reference
    unique_claim = models.BooleanField(default=False)
    # Marks this denial as a good example for training or reference
    disability_claim = models.BooleanField(default=False)
    good_appeal_example = models.BooleanField(default=False)
    candidate_procedure = models.CharField(max_length=300, null=True, blank=True)
    candidate_diagnosis = models.CharField(max_length=300, null=True, blank=True)
    candidate_generated_questions = models.JSONField(null=True, blank=True)
    candidate_ml_citation_context = models.JSONField(null=True, blank=True)
    gen_attempts = models.IntegerField(null=True, default=0)
    # Track which microsite the user came from (if any)
    microsite_slug = models.CharField(
        max_length=100, null=True, blank=True, db_index=True
    )
    # Track where the user heard about the service
    referral_source = models.CharField(max_length=300, null=True, blank=True)
    referral_source_details = models.TextField(null=True, blank=True)

    @classmethod
    def filter_to_allowed_denials(cls, current_user: User):
        if current_user.is_superuser or current_user.is_staff:
            return Denial.objects.all()

        query_set = Denial.objects.none()

        # Patients can view their own appeals
        try:
            patient_user = PatientUser.objects.get(user=current_user, active=True)
            if patient_user and patient_user.active:
                query_set |= Denial.objects.filter(
                    patient_user=patient_user,
                    patient_visible=True,
                )
        except PatientUser.DoesNotExist:
            pass

        # Providers can view appeals they created or were added to as a provider
        # or are a domain admin in.
        try:
            # Appeals they created
            professional_user = ProfessionalUser.objects.get(
                user=current_user, active=True
            )
            query_set |= Denial.objects.filter(primary_professional=professional_user)
            query_set |= Denial.objects.filter(creating_professional=professional_user)
            # Appeals they were add to.
            additional = SecondaryDenialProfessionalRelation.objects.filter(
                professional=professional_user
            )
            query_set |= Denial.objects.filter(pk__in=[a.denial.pk for a in additional])
            # Practice/UserDomain admins can view all appeals in their practice
            try:
                user_admin_domains = professional_user.admin_domains()
                query_set |= Denial.objects.filter(domain__in=user_admin_domains)
            except ProfessionalDomainRelation.DoesNotExist:
                pass
        except ProfessionalUser.DoesNotExist:
            pass
        return query_set

    def follow_up(self):
        return self.raw_email is not None and "@" in self.raw_email

    def chose_appeal(self):
        return self.appeal_text is not None and len(self.appeal_text) > 10

    def __str__(self):
        return f"{self.denial_id} -- {self.date} -- Follow Up: {self.follow_up()} -- Chose Appeal {self.chose_appeal()}"

    @staticmethod
    def get_hashed_email(email: str) -> str:
        encoded_email = email.encode("utf-8").lower()
        return hashlib.sha512(encoded_email).hexdigest()


class DenialQA(models.Model):
    """
    Stores question-answer pairs for a denial.
    Used to capture additional context gathered during the appeal generation process.
    """

    id = models.AutoField(primary_key=True)
    denial = models.ForeignKey("Denial", on_delete=models.CASCADE)
    question = models.TextField(max_length=3000, primary_key=False)
    text_answer = models.TextField(max_length=3000, primary_key=False)
    bool_answer = models.BooleanField(default=False)


class ProposedAppeal(ExportModelOperationsMixin("ProposedAppeal"), models.Model):  # type: ignore
    """
    Stores AI-generated appeal letter proposals for users to review and select.
    Multiple proposals may be generated for a single denial.
    """

    appeal_text = models.TextField(max_length=3000000000, null=True, blank=True)
    for_denial = models.ForeignKey(
        Denial, on_delete=models.CASCADE, null=True, blank=True
    )
    chosen = models.BooleanField(default=False)
    editted = models.BooleanField(default=False)

    def __str__(self):
        if self.appeal_text is not None:
            return f"{self.appeal_text[0:100]}"
        else:
            return f"{self.appeal_text}"


class Appeal(ExportModelOperationsMixin("Appeal"), models.Model):  # type: ignore
    """
    Represents a finalized appeal letter that has been or will be submitted.
    Links to the original denial, associated chat, and fax sending records.
    Supports access control filtering similar to Denial model.
    """

    id = models.AutoField(primary_key=True)
    uuid = models.CharField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
        db_index=False,
        max_length=100,
    )
    appeal_text = models.TextField(max_length=3000000000, null=True, blank=True)
    for_denial = models.ForeignKey(
        Denial, on_delete=models.CASCADE, null=True, blank=True
    )
    chat = models.ForeignKey(
        "OngoingChat",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="appeals",
        db_index=True,
    )
    hashed_email = models.CharField(max_length=300, primary_key=False)
    creating_professional = models.ForeignKey(
        ProfessionalUser,
        null=True,
        on_delete=models.SET_NULL,
        related_name="appeals_created",
    )
    primary_professional = models.ForeignKey(
        ProfessionalUser,
        null=True,
        on_delete=models.SET_NULL,
        related_name="appeals_primary",
    )
    patient_user = models.ForeignKey(PatientUser, null=True, on_delete=models.SET_NULL)
    domain = models.ForeignKey(UserDomain, null=True, on_delete=models.SET_NULL)
    document_enc = EncryptedFileField(null=True, storage=settings.COMBINED_STORAGE)
    # TODO: Use signals on pending
    pending = models.BooleanField(default=True)
    pending_patient = models.BooleanField(default=False)
    pending_professional = models.BooleanField(default=True)
    sent = models.BooleanField(default=False)
    fax = models.ForeignKey("FaxesToSend", on_delete=models.SET_NULL, null=True)
    # Who do we want to send the appeal?
    professional_send = models.BooleanField(default=True)
    patient_send = models.BooleanField(default=True)
    patient_visible = models.BooleanField(default=True)
    # Pubmed IDs for the articles to be included in the appeal
    pubmed_ids_json = models.JSONField(blank=True, null=True)
    response_document_enc = EncryptedFileField(
        null=True, storage=settings.COMBINED_STORAGE
    )
    response_text = models.TextField(max_length=3000000000, null=True)
    response_date = models.DateField(auto_now=False, null=True)
    # Notes for the professional
    notes = models.TextField(max_length=3000000000, null=True, blank=True)
    # Track whether an appeal was successful (not just if it got a response)
    success = models.BooleanField(default=False, null=True)
    mod_date = models.DateField(auto_now=True, null=True)
    creation_date = models.DateField(auto_now_add=True, null=True)
    billed = models.BooleanField(default=False)
    include_provided_health_history_in_appeal = models.BooleanField(
        default=False, null=True
    )

    def details(self):
        ml_citation_context = (
            self.for_denial.ml_citation_context
            if self.for_denial and hasattr(self.for_denial, "ml_citation_context")
            else None
        )
        qa_context = (
            self.for_denial.qa_context
            if self.for_denial and hasattr(self.for_denial, "qa_context")
            else None
        )

        patient_extra = ""
        if self.patient_user is not None:
            patient_extra = f" -- {self.patient_user.user.username}"

        return f"""
        appeal id: {self.id}
        internal notes: {self.notes}
        modification date: {self.mod_date}
        creation date: {self.creation_date}
        patient visible: {self.patient_visible}
        include provided health history in appeal: {self.include_provided_health_history_in_appeal}
        possible citation context: {ml_citation_context}
        qa context: {qa_context}
        {patient_extra}
        """

    @classmethod
    def get_optional_for_user(cls, current_user: User, id):
        return (
            cls.filter_to_allowed_appeals(current_user)
            .select_related("for_denial")
            .filter(id=id)
            .first()
        )

    # Similar to the method on denial -- TODO refactor to a mixin / DRY
    @classmethod
    def filter_to_allowed_appeals(cls, current_user: User):
        if current_user.is_superuser or current_user.is_staff:
            return Appeal.objects.all()

        query_set = Appeal.objects.none()

        # Patients can view their own appeals
        try:
            patient_user = PatientUser.objects.get(user=current_user, active=True)
            if patient_user and patient_user.active:
                query_set |= Appeal.objects.filter(
                    patient_user=patient_user,
                    patient_visible=True,
                )
        except PatientUser.DoesNotExist:
            pass

        # Providers can view appeals they created or were added to as a provider
        # or are a domain admin in.
        try:
            # Appeals they created
            professional_user = ProfessionalUser.objects.get(
                user=current_user, active=True
            )
            query_set |= Appeal.objects.filter(primary_professional=professional_user)
            query_set |= Appeal.objects.filter(creating_professional=professional_user)
            # Appeals they were add to.
            additional_appeals = SecondaryAppealProfessionalRelation.objects.filter(
                professional=professional_user
            )
            query_set |= Appeal.objects.filter(
                id__in=[a.appeal.id for a in additional_appeals]
            )
            # Practice/UserDomain admins can view all appeals in their practice
            try:
                user_admin_domains = professional_user.admin_domains()
                query_set |= Appeal.objects.filter(domain__in=user_admin_domains)
            except ProfessionalDomainRelation.DoesNotExist:
                pass
        except ProfessionalUser.DoesNotExist:
            pass
        return query_set

    def __str__(self):
        if self.appeal_text is not None:
            return f"{self.uuid} -- {self.appeal_text[0:100]}"
        else:
            return f"{self.uuid} -- {self.appeal_text}"


# Secondary relations for denials and appeals
# Secondary Appeal Relations
class SecondaryAppealProfessionalRelation(models.Model):
    """Links additional professionals to appeals beyond the primary/creating professional."""

    appeal = models.ForeignKey(Appeal, on_delete=models.CASCADE)
    professional = models.ForeignKey(ProfessionalUser, on_delete=models.CASCADE)


# Seconday Denial Relations
class SecondaryDenialProfessionalRelation(models.Model):
    """Links additional professionals to denials beyond the primary/creating professional."""

    denial = models.ForeignKey(Denial, on_delete=models.CASCADE)
    professional = models.ForeignKey(ProfessionalUser, on_delete=models.CASCADE)


# Stripe


class StripeProduct(models.Model):
    """Stores Stripe product information for billing integration."""

    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=300)
    stripe_id = models.CharField(max_length=300)
    active = models.BooleanField(default=True)


class StripePrice(models.Model):
    """Stores Stripe price information linked to products."""

    id = models.AutoField(primary_key=True)
    product = models.ForeignKey(StripeProduct, on_delete=models.CASCADE)
    stripe_id = models.CharField(max_length=300)
    amount = models.IntegerField()
    currency = models.CharField(max_length=3)
    active = models.BooleanField(default=True)


class StripeMeter(models.Model):
    """Stores Stripe meter information for usage-based billing."""

    id = models.AutoField(primary_key=True)
    stripe_meter_id = models.CharField(max_length=300)
    name = models.CharField(max_length=300)
    active = models.BooleanField(default=True)


class AppealAttachment(models.Model):
    """
    Stores file attachments for appeals (e.g., medical records, supporting documents).
    Files are encrypted at rest for privacy protection.
    """

    appeal = models.ForeignKey(
        Appeal, on_delete=models.CASCADE, related_name="attachments"
    )
    file = EncryptedFileField(null=True, storage=settings.COMBINED_STORAGE)
    filename = models.CharField(max_length=255)
    mime_type = models.CharField(max_length=127)
    created_at = models.DateTimeField(auto_now_add=True)

    @classmethod
    def filter_to_allowed_attachments(cls, user):
        """Filter attachments to only those the user has permission to access"""
        allowed_appeals = Appeal.filter_to_allowed_appeals(user)
        return cls.objects.filter(appeal__in=allowed_appeals)


class StripeRecoveryInfo(models.Model):
    """Stores recovery information for failed Stripe transactions."""

    id = models.AutoField(primary_key=True)
    items = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)


class LostStripeSession(models.Model):
    """Tracks Stripe checkout sessions that were started but not completed."""

    id = models.AutoField(primary_key=True)
    session_id = models.CharField(max_length=255, null=True, blank=True)
    payment_type = models.CharField(max_length=255, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, blank=True)
    cancel_url = models.CharField(max_length=255, null=True, blank=True)
    success_url = models.CharField(max_length=255, null=True, blank=True)
    email = models.CharField(max_length=255, null=True, blank=True)
    metadata = models.JSONField(null=True, blank=True)


class LostStripeMeters(models.Model):
    """Tracks Stripe meter events that failed to be recorded for later retry."""

    id = models.AutoField(primary_key=True)
    created_at = models.DateTimeField(auto_now_add=True, blank=True)
    payload = models.JSONField(null=True, blank=True)
    resubmitted = models.BooleanField(default=False)
    error = models.CharField(max_length=300)


class StripeWebhookEvents(models.Model):
    """Logs Stripe webhook events for idempotency and debugging."""

    internal_id = models.AutoField(primary_key=True)
    event_stripe_id = models.CharField(max_length=255, null=False)
    created_at = models.DateTimeField(auto_now_add=True, blank=True)
    success = models.BooleanField(default=False)
    error = models.CharField(max_length=255, null=True, blank=True)


class PriorAuthRequest(ExportModelOperationsMixin("PriorAuthRequest"), models.Model):  # type: ignore
    """
    Stores information about a prior authorization request.
    Used to track the status of the request and store the questions and answers.
    Supports guided and raw modes with status tracking through the workflow.
    """

    chat = models.ForeignKey(
        "OngoingChat",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="prior_auths",
        db_index=True,
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    creator_professional_user = models.ForeignKey(
        ProfessionalUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="prior_auth_requests_creators",
    )
    created_for_professional_user = models.ForeignKey(
        ProfessionalUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="prior_auth_requests_created_for",
    )
    domain = models.ForeignKey(
        UserDomain, on_delete=models.SET_NULL, null=True, blank=True
    )

    # Medical information
    diagnosis = models.TextField()
    treatment = models.TextField()
    insurance_company = models.TextField()
    patient_health_history = models.TextField(blank=True)
    urgent = models.BooleanField(default=False)

    # Patient information
    patient_name = models.TextField(blank=True, null=True)
    plan_id = models.TextField(blank=True, null=True)
    member_id = models.TextField(blank=True, null=True)
    patient_dob = models.DateField(null=True, blank=True)

    # Mode selection for the request
    MODE_CHOICES = (
        ("guided", "Guided"),
        ("raw", "Raw"),
    )
    mode = models.CharField(max_length=10, choices=MODE_CHOICES, default="guided")

    # Letter or case note
    PROPOSAL_TYPE = (
        ("letter", "Letter"),
        ("case_note", "Case Note"),
    )

    proposal_type = models.CharField(
        max_length=10, choices=PROPOSAL_TYPE, default="letter"
    )

    # Q&A for the request
    questions = models.JSONField(null=True, blank=True)  # List of [(question, default)]
    answers = models.JSONField(null=True, blank=True)  # Dict of {question: answer}

    # Status tracking
    STATUS_CHOICES = (
        ("initial", "Initial"),
        ("questions_asked", "Questions Asked"),
        ("questions_answered", "Questions Answered"),
        ("prior_auth_requested", "Prior Auth Requested"),
        ("completed", "Completed"),
    )
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default="initial")

    # Security token for WebSocket access
    token = models.UUIDField(default=uuid.uuid4, editable=False)

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Final text chosen
    text = models.TextField(blank=True, null=True)

    def details(self):
        return f"""
        prior auth id: {self.id}
        diagnosis: {self.diagnosis}
        treatment: {self.treatment}
        insurance company: {self.insurance_company}
        patient health history: {self.patient_health_history}
        urgent: {self.urgent}
        answers: {self.answers}
        proposal type: {self.proposal_type}
        prior auth text: {self.text}"""

    @classmethod
    def filter_to_allowed_requests(cls, current_user):
        """Filter to requests that the current user is allowed to see."""
        if not current_user.is_authenticated:
            return cls.objects.none()

        try:
            professional_user = ProfessionalUser.objects.get(user=current_user)

            # Requests created by the user or created for the user
            return cls.objects.filter(
                Q(creator_professional_user=professional_user)
                | Q(created_for_professional_user=professional_user)
            )
        except ProfessionalUser.DoesNotExist:
            return cls.objects.none()

    @classmethod
    def get_optional_for_user(cls, current_user: User, id):
        return cls.filter_to_allowed_requests(current_user).filter(id=id).first()

    def __str__(self):
        return f"Prior Auth Request {self.id} - {self.status}"


class ProposedPriorAuth(ExportModelOperationsMixin("ProposedPriorAuth"), models.Model):  # type: ignore
    """
    Model for proposed prior authorization text, similar to ProposedAppeal.
    """

    proposed_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    prior_auth_request = models.ForeignKey(PriorAuthRequest, on_delete=models.CASCADE)
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    selected = models.BooleanField(default=False)

    def __str__(self):
        return (
            f"Proposed Prior Auth {self.proposed_id} for {self.prior_auth_request.id}"
        )


class OngoingChat(models.Model):
    """
    Model for storing ongoing chat sessions between users and LLM.
    Can be associated with either professional users, regular users (patients),
    or anonymous users (via session).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    professional_user = models.ForeignKey(
        ProfessionalUser, on_delete=models.SET_NULL, null=True, blank=True
    )
    user = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="chats"
    )
    is_patient = models.BooleanField(default=False)
    # For anonymous users (not logged in), track by session ID
    session_key = models.CharField(max_length=100, null=True, blank=True, db_index=True)
    chat_history = models.JSONField(
        default=list, null=True, blank=True
    )  # JSON List of dictionaries {"role": "user", "content": message, "timestamp": timezone.now().isoformat()})
    summary_for_next_call = models.JSONField(
        null=True, blank=True
    )  # JSON List of strings
    denied_item = models.CharField(
        max_length=500,
        null=True,
        blank=True,
        help_text="The item that was denied, extracted from chat context",
    )
    denied_reason = models.CharField(
        max_length=500,
        null=True,
        blank=True,
        help_text="The reason for denial, extracted from chat context",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    domain = models.ForeignKey(
        UserDomain, null=True, on_delete=models.SET_NULL, blank=True
    )
    # Hashed email for the user, used for anonymization and privacy
    hashed_email = models.CharField(
        max_length=300, null=True, blank=True, help_text="Hashed email of the user"
    )
    # Track which microsite the user came from (if any)
    microsite_slug = models.CharField(
        max_length=100, null=True, blank=True, db_index=True
    )

    @staticmethod
    def find_chats_by_email(email: str):
        """
        Find all chats associated with an email address.
        Used for data deletion requests and privacy compliance.
        """
        if not email:
            return OngoingChat.objects.none()

        # Hash the email for lookup
        hashed_email = Denial.get_hashed_email(email)

        # Look for chats with matching hashed email
        return OngoingChat.objects.filter(
            Q(hashed_email=hashed_email)
            | Q(user__email=email)
            | Q(professional_user__user__email=email)
        )

    def summarize_professional_user(self) -> str:
        if self.is_patient and self.user:
            return f"a patient user (ID: {self.user.id})"
        elif self.professional_user:
            return str(self.professional_user)
        else:
            return "a user"

    def summarize_user(self) -> str:
        """Returns a description of the user - either patient, professional, or anonymous."""
        if self.is_patient and self.user:
            return f"a patient user (ID: {self.user.id})"
        elif self.professional_user:
            return f"a professional user ({self.professional_user.get_display_name()})"
        elif self.session_key:
            return f"an anonymous user (Session: {self.session_key[:8]})"
        else:
            return "a user"

    def __str__(self):
        if self.is_patient and self.user:
            return f"Ongoing Chat {self.id} for patient {self.user.email}"
        elif self.professional_user:
            return f"Ongoing Chat {self.id} for {self.professional_user.get_display_name()}"
        elif self.session_key:
            return (
                f"Ongoing Chat {self.id} for anonymous session {self.session_key[:8]}"
            )
        else:
            return f"Ongoing Chat {self.id} (no associated user)"

    def is_professional_user(self):
        return not self.is_patient

    def is_logged_in_user(self):
        return self.user is not None


class ChatLeads(ExportModelOperationsMixin("ChatLeads"), models.Model):  # type: ignore
    """
    Stores lead data from trial chat users who have not created a full account.
    Used to track trial chat usage and for follow-up marketing.
    """

    name = models.CharField(max_length=255)
    email = models.EmailField()
    phone = models.CharField(max_length=32)
    company = models.CharField(max_length=255)
    consent_to_contact = models.BooleanField()  # Required
    agreed_to_terms = models.BooleanField()  # Required
    session_id = models.CharField(max_length=255, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    drug = models.CharField(max_length=255, null=True, blank=True)
    # Track which microsite the user came from (if any)
    microsite_slug = models.CharField(
        max_length=100, null=True, blank=True, db_index=True
    )
    referral_source = models.CharField(max_length=300, null=True, blank=True)
    referral_source_details = models.TextField(null=True, blank=True)

    class Meta:
        verbose_name = "Chat Lead"
        verbose_name_plural = "Chat Leads"

    def __str__(self):
        return f"Chat Lead: {self.name} ({self.email})"


class ChooserTask(ExportModelOperationsMixin("ChooserTask"), models.Model):  # type: ignore
    """
    Model for chooser tasks that present multiple synthetic candidates for users to choose from.
    Used to collect preference data for training selector models.
    """

    TASK_TYPE_CHOICES = [
        ("appeal", "Appeal Letter"),
        ("chat", "Chat Response"),
    ]

    STATUS_CHOICES = [
        ("QUEUED", "Queued"),
        ("READY", "Ready"),
        ("IN_USE", "In Use"),
        ("EXHAUSTED", "Exhausted"),
        ("DISABLED", "Disabled"),
    ]

    id = models.AutoField(primary_key=True)
    task_type = models.CharField(max_length=20, choices=TASK_TYPE_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="QUEUED")

    # Normalized JSON context for flexibility (contains denial summary or chat prompt)
    context_json = models.JSONField(null=True, blank=True)

    source = models.CharField(max_length=50, default="synthetic")
    num_candidates_expected = models.IntegerField(default=4)
    num_candidates_generated = models.IntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["task_type", "status"]),
            models.Index(fields=["status", "created_at"]),
        ]
        verbose_name = "Chooser Task"
        verbose_name_plural = "Chooser Tasks"

    def __str__(self):
        return f"ChooserTask {self.id} ({self.task_type}, {self.status})"


class ChooserCandidate(ExportModelOperationsMixin("ChooserCandidate"), models.Model):  # type: ignore
    """
    Model for individual candidates within a ChooserTask.
    Each task will have 2-4 candidates for users to choose from.
    """

    KIND_CHOICES = [
        ("appeal_letter", "Appeal Letter"),
        ("chat_response", "Chat Response"),
    ]

    id = models.AutoField(primary_key=True)
    task = models.ForeignKey(
        ChooserTask, on_delete=models.CASCADE, related_name="candidates"
    )
    candidate_index = models.IntegerField()  # 0, 1, 2, 3
    kind = models.CharField(max_length=30, choices=KIND_CHOICES)
    model_name = models.CharField(max_length=200)
    content = models.TextField()
    metadata = models.JSONField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [["task", "candidate_index"]]
        ordering = ["task", "candidate_index"]
        verbose_name = "Chooser Candidate"
        verbose_name_plural = "Chooser Candidates"

    def __str__(self):
        return f"Candidate {self.candidate_index} for Task {self.task_id}"


class ChooserVote(ExportModelOperationsMixin("ChooserVote"), models.Model):  # type: ignore
    """
    Model to store user votes selecting the best candidate from a ChooserTask.
    """

    id = models.AutoField(primary_key=True)
    task = models.ForeignKey(
        ChooserTask, on_delete=models.CASCADE, related_name="votes"
    )
    chosen_candidate = models.ForeignKey(
        ChooserCandidate, on_delete=models.CASCADE, related_name="votes"
    )
    presented_candidate_ids = models.JSONField()  # List of candidate IDs shown to user
    session_key = models.CharField(max_length=100, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # Prevent same session from voting on same task multiple times
        unique_together = [["task", "session_key"]]
        indexes = [
            models.Index(fields=["task"]),
            models.Index(fields=["session_key"]),
        ]
        verbose_name = "Chooser Vote"
        verbose_name_plural = "Chooser Votes"

    def __str__(self):
        """
        Return a human-readable label for the vote that includes the chosen candidate ID and the task ID.

        Returns:
            str: A string in the form "Vote for Candidate {chosen_candidate_id} on Task {task_id}".
        """
        return f"Vote for Candidate {self.chosen_candidate_id} on Task {self.task_id}"


class ChooserSkip(ExportModelOperationsMixin("ChooserSkip"), models.Model):  # type: ignore
    """
    Model to track tasks that users have skipped.
    This helps ensure users don't see the same task repeatedly.
    """

    id = models.AutoField(primary_key=True)
    task = models.ForeignKey(
        ChooserTask, on_delete=models.CASCADE, related_name="skips"
    )
    session_key = models.CharField(max_length=100, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # Prevent same session from skipping same task multiple times
        unique_together = [["task", "session_key"]]
        indexes = [
            models.Index(fields=["task"]),
            models.Index(fields=["session_key"]),
        ]
        verbose_name = "Chooser Skip"
        verbose_name_plural = "Chooser Skips"

    def __str__(self):
        """
        Human-readable representation of this ChooserSkip showing the associated task and session.

        Returns:
            str: A string in the form "Skip of Task {task_id} by {session_key}".
        """
        return f"Skip of Task {self.task_id} by {self.session_key}"


# Policy Document Analysis Models (Issue #570)
class PolicyDocument(ExportModelOperationsMixin("PolicyDocument"), models.Model):  # type: ignore
    """
    Stores uploaded policy documents (Summary of Benefits, Medical Policy PDFs).
    Used to help users understand their insurance coverage.
    """

    DOCUMENT_TYPE_CHOICES = [
        ("summary_of_benefits", "Summary of Benefits"),
        ("medical_policy", "Medical Policy"),
        ("other", "Other"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.FileField(upload_to="policy_docs/", blank=True)
    document_enc = EncryptedFileField(upload_to="policy_docs_enc/", blank=True)
    document_type = models.CharField(
        max_length=50, choices=DOCUMENT_TYPE_CHOICES, default="other"
    )
    filename = models.CharField(max_length=255, blank=True)
    hashed_email = models.CharField(max_length=200, blank=True, db_index=True)
    session_key = models.CharField(max_length=100, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    raw_text = models.TextField(blank=True)  # Extracted text from PDF

    class Meta:
        indexes = [
            models.Index(fields=["hashed_email"]),
            models.Index(fields=["session_key"]),
            models.Index(fields=["created_at"]),
        ]
        verbose_name = "Policy Document"
        verbose_name_plural = "Policy Documents"

    def __str__(self):
        return f"PolicyDocument: {self.filename or self.id} ({self.document_type})"


class PolicyDocumentAnalysis(ExportModelOperationsMixin("PolicyDocumentAnalysis"), models.Model):  # type: ignore
    """
    Stores AI analysis of policy documents.
    Includes extracted exclusions, inclusions, and appeal-relevant clauses.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    policy_document = models.ForeignKey(
        PolicyDocument, on_delete=models.CASCADE, related_name="analyses"
    )
    user_question = models.TextField(blank=True)  # What user wanted to understand
    exclusions = models.JSONField(default=list)  # List of exclusion clauses
    inclusions = models.JSONField(default=list)  # List of coverage/inclusion clauses
    appeal_clauses = models.JSONField(default=list)  # Clauses relevant to appeals
    summary = models.TextField(blank=True)  # Overall summary
    quotable_sections = models.JSONField(default=list)  # Formatted quotes with page refs
    created_at = models.DateTimeField(auto_now_add=True)
    chat = models.ForeignKey(
        "OngoingChat", on_delete=models.SET_NULL, null=True, blank=True, related_name="policy_analyses"
    )

    class Meta:
        indexes = [
            models.Index(fields=["policy_document"]),
            models.Index(fields=["created_at"]),
        ]
        verbose_name = "Policy Document Analysis"
        verbose_name_plural = "Policy Document Analyses"

    def __str__(self):
        return f"Analysis of {self.policy_document.filename or self.policy_document_id}"
