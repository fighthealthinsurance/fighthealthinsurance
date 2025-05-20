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
    id = models.AutoField(primary_key=True)
    email = models.EmailField()
    name = models.CharField(max_length=300, default="", blank=True)
    comments = models.TextField(default="", blank=True)
    signup_date = models.DateField(auto_now_add=True)
    phone = models.CharField(max_length=300, default="", blank=True)

    def __str__(self):
        return self.email


class FollowUpType(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=300, default="")
    subject = models.CharField(max_length=300, primary_key=False)
    text = models.TextField(max_length=30000, primary_key=False)
    duration = models.DurationField()

    def __str__(self):
        return self.name


class FollowUp(models.Model):
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
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=100)

    def __str__(self):
        return f"{self.id}:{self.name}"


class PlanDocuments(models.Model):
    plan_document_id = models.AutoField(primary_key=True)
    plan_document = models.FileField(null=True, storage=settings.COMBINED_STORAGE)
    plan_document_enc = EncryptedFileField(null=True, storage=settings.COMBINED_STORAGE)
    # If the denial is deleted it's either SPAM or a removal request in either case
    # we cascade the delete
    denial = models.ForeignKey("Denial", on_delete=models.CASCADE)


class FollowUpDocuments(models.Model):
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
    internal_id = models.AutoField(primary_key=True)
    query = models.TextField(null=False, max_length=300)
    since = models.TextField(null=True)  # text date for the since query
    articles = models.TextField(null=True)  # json
    query_date = models.DateTimeField(auto_now_add=True)
    denial_id = models.ForeignKey("Denial", on_delete=models.SET_NULL, null=True)
    created = models.DateTimeField(db_default=Now(), null=True)


class FaxesToSend(ExportModelOperationsMixin("FaxesToSend"), models.Model):  # type: ignore
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
    denial = models.ForeignKey("Denial", on_delete=models.CASCADE)
    denial_type = models.ForeignKey(DenialTypes, on_delete=models.CASCADE)
    src = models.ForeignKey(DataSource, on_delete=models.SET_NULL, null=True)


class PlanTypesRelation(models.Model):
    denial = models.ForeignKey("Denial", on_delete=models.CASCADE)
    plan_type = models.ForeignKey(PlanType, on_delete=models.CASCADE)
    src = models.ForeignKey(DataSource, on_delete=models.SET_NULL, null=True)


class PlanSourceRelation(models.Model):
    denial = models.ForeignKey("Denial", on_delete=models.CASCADE)
    plan_source = models.ForeignKey(PlanSource, on_delete=models.CASCADE)
    src = models.ForeignKey(DataSource, on_delete=models.SET_NULL, null=True)


class Denial(ExportModelOperationsMixin("Denial"), models.Model):  # type: ignore
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
    id = models.AutoField(primary_key=True)
    denial = models.ForeignKey("Denial", on_delete=models.CASCADE)
    question = models.TextField(max_length=3000, primary_key=False)
    text_answer = models.TextField(max_length=3000, primary_key=False)
    bool_answer = models.BooleanField(default=False)


class ProposedAppeal(ExportModelOperationsMixin("ProposedAppeal"), models.Model):  # type: ignore
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
    appeal = models.ForeignKey(Appeal, on_delete=models.CASCADE)
    professional = models.ForeignKey(ProfessionalUser, on_delete=models.CASCADE)


# Seconday Denial Relations
class SecondaryDenialProfessionalRelation(models.Model):
    denial = models.ForeignKey(Denial, on_delete=models.CASCADE)
    professional = models.ForeignKey(ProfessionalUser, on_delete=models.CASCADE)


# Stripe


class StripeProduct(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=300)
    stripe_id = models.CharField(max_length=300)
    active = models.BooleanField(default=True)


class StripePrice(models.Model):
    id = models.AutoField(primary_key=True)
    product = models.ForeignKey(StripeProduct, on_delete=models.CASCADE)
    stripe_id = models.CharField(max_length=300)
    amount = models.IntegerField()
    currency = models.CharField(max_length=3)
    active = models.BooleanField(default=True)


class StripeMeter(models.Model):
    id = models.AutoField(primary_key=True)
    stripe_meter_id = models.CharField(max_length=300)
    name = models.CharField(max_length=300)
    active = models.BooleanField(default=True)


class AppealAttachment(models.Model):
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
    id = models.AutoField(primary_key=True)
    items = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)


class LostStripeSession(models.Model):
    id = models.AutoField(primary_key=True)
    session_id = models.CharField(max_length=255, null=True, blank=True)
    payment_type = models.CharField(max_length=255, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, blank=True)
    cancel_url = models.CharField(max_length=255, null=True, blank=True)
    success_url = models.CharField(max_length=255, null=True, blank=True)
    email = models.CharField(max_length=255, null=True, blank=True)
    metadata = models.JSONField(null=True, blank=True)


class LostStripeMeters(models.Model):
    id = models.AutoField(primary_key=True)
    created_at = models.DateTimeField(auto_now_add=True, blank=True)
    payload = models.JSONField(null=True, blank=True)
    resubmitted = models.BooleanField(default=False)
    error = models.CharField(max_length=300)


class StripeWebhookEvents(models.Model):
    internal_id = models.AutoField(primary_key=True)
    event_stripe_id = models.CharField(max_length=255, null=False)
    created_at = models.DateTimeField(auto_now_add=True, blank=True)
    success = models.BooleanField(default=False)
    error = models.CharField(max_length=255, null=True, blank=True)


class PriorAuthRequest(ExportModelOperationsMixin("PriorAuthRequest"), models.Model):  # type: ignore
    chat = models.ForeignKey(
        "OngoingChat",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="prior_auths",
        db_index=True,
    )
    """
    Stores information about a prior authorization request.
    Used to track the status of the request and store the questions and answers.
    """

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
