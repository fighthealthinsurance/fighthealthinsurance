import hashlib
import os
import re
import sys
import tempfile
import uuid
import typing

from django.conf import settings
from django.db import models
from django.db.models.functions import Now
from django_prometheus.models import ExportModelOperationsMixin
from django_encrypted_filefield.fields import EncryptedFileField
from django.contrib.auth import get_user_model

from fighthealthinsurance.utils import sekret_gen
from fhi_users.models import *
from regex_field.fields import RegexField

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


# Money related :p
class InterestedProfessional(ExportModelOperationsMixin("InterestedProfessional"), models.Model):  # type: ignore
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=300, primary_key=False, default="", blank=True)
    business_name = models.CharField(
        max_length=300, primary_key=False, default="", blank=True
    )
    phone_number = models.CharField(
        max_length=300, primary_key=False, default="", blank=True
    )
    address = models.CharField(
        max_length=1000, primary_key=False, default="", blank=True
    )
    email = models.EmailField()
    comments = models.TextField(primary_key=False, default="", blank=True)
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
    name = models.CharField(max_length=300, primary_key=False, default="", blank=True)
    comments = models.TextField(primary_key=False, default="", blank=True)
    signup_date = models.DateField(auto_now_add=True)

    def __str__(self):
        return self.email


class FollowUpType(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=300, primary_key=False, default="")
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
    user_comments = models.TextField(primary_key=False, null=True)
    quote = models.TextField(primary_key=False, null=True)
    use_quote = models.BooleanField(default=False)
    email = models.CharField(max_length=300, null=True)
    name_for_quote = models.TextField(primary_key=False, null=True)
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
    alt_name = models.CharField(max_length=300, primary_key=False, blank=True)
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
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=300, primary_key=False)
    regex = RegexField(max_length=400, re_flags=re.IGNORECASE | re.UNICODE | re.M)

    def __str__(self):
        return f"{self.id}:{self.name}"


class Procedures(models.Model):
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
    appeal_text = models.TextField(max_length=3000, primary_key=False, blank=True)
    form = models.CharField(max_length=300, null=True, blank=True)

    def get_form(self):
        if self.form is None:
            parent = self.parent
            if parent is not None:
                return parent.get_form()
            else:
                return None
        else:
            try:
                return getattr(
                    sys.modules["fighthealthinsurance.forms.questions"], self.form
                )
            except Exception as e:
                print(f"Error loading form {e}")
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
    appeal_text = models.TextField(max_length=3000, primary_key=False, blank=True)

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

    pmid = models.TextField(primary_key=False, blank=True)
    doi = models.TextField(primary_key=False, blank=True, null=True)
    query = models.TextField(primary_key=False, blank=True)
    title = models.TextField(blank=True, null=True)
    abstract = models.TextField(primary_key=False, blank=True, null=True)
    text = models.TextField(primary_key=False, blank=True, null=True)
    basic_summary = models.TextField(primary_key=False, blank=True, null=True)
    says_effective = models.BooleanField(null=True, blank=True)
    publication_date = models.DateTimeField(null=True, blank=True)
    retrival_date = models.TextField(blank=True, null=True)
    article_url = models.TextField(primary_key=False, blank=True, null=True)


class PubMedQueryData(models.Model):
    internal_id = models.AutoField(primary_key=True)
    query = models.TextField(null=False, max_length=300)
    articles = models.TextField(null=True)  # json
    query_date = models.DateTimeField(auto_now_add=True)
    denial_id = models.ForeignKey("Denial", on_delete=models.SET_NULL, null=True)


class FaxesToSend(ExportModelOperationsMixin("FaxesToSend"), models.Model):  # type: ignore
    fax_id = models.AutoField(primary_key=True)
    hashed_email = models.CharField(max_length=300, primary_key=False)
    date = models.DateTimeField(auto_now=False, auto_now_add=True)
    paid = models.BooleanField()
    email = models.CharField(max_length=300)
    name = models.CharField(max_length=300, null=True)
    appeal_text = models.TextField()
    pmids = models.CharField(max_length=600, blank=True)
    health_history = models.TextField(null=True, blank=True)
    combined_document = models.FileField(null=True, storage=settings.COMBINED_STORAGE)
    combined_document_enc = EncryptedFileField(
        null=True, storage=settings.COMBINED_STORAGE
    )
    uuid = models.CharField(
        max_length=300, primary_key=False, default=uuid.uuid4, editable=False
    )
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

    def get_temporary_document_path(self):
        combined_document = self.combined_document or self.combined_document_enc
        with tempfile.NamedTemporaryFile(
            suffix=combined_document.name, mode="w+b", delete=False
        ) as f:
            f.write(combined_document.read())
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
    uuid = models.CharField(
        max_length=300, primary_key=False, default=uuid.uuid4, editable=False
    )
    hashed_email = models.CharField(max_length=300, primary_key=False)
    denial_text = models.TextField(primary_key=False)
    denial_type_text = models.TextField(
        max_length=200, primary_key=False, null=True, blank=True
    )
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
    insurance_company = models.CharField(
        max_length=300, primary_key=False, null=True, blank=True
    )
    claim_id = models.CharField(
        max_length=300, primary_key=False, null=True, blank=True
    )
    procedure = models.CharField(max_length=300, primary_key=False, null=True)
    diagnosis = models.CharField(max_length=300, primary_key=False, null=True)
    # Keep track of if the async thread finished extracting procedure and diagnosis
    extract_procedure_diagnosis_finished = models.BooleanField(default=False, null=True)
    appeal_text = models.TextField(primary_key=False, null=True, blank=True)
    raw_email = models.TextField(
        max_length=300, primary_key=False, null=True, blank=True
    )
    created = models.DateTimeField(db_default=Now(), primary_key=False, null=True)
    use_external = models.BooleanField(default=False)
    health_history = models.TextField(primary_key=False, null=True, blank=True)
    qa_context = models.TextField(primary_key=False, null=True, blank=True)
    plan_context = models.TextField(primary_key=False, null=True, blank=True)
    semi_sekret = models.CharField(max_length=100, default=sekret_gen)
    plan_id = models.CharField(max_length=200, primary_key=False, null=True)
    state = models.CharField(max_length=4, primary_key=False, null=True, blank=True)
    appeal_result = models.CharField(max_length=200, null=True)
    last_interaction = models.DateTimeField(auto_now=True)
    follow_up_semi_sekret = models.CharField(max_length=100, default=sekret_gen)
    references = models.TextField(primary_key=False, null=True, blank=True)
    reference_summary = models.TextField(primary_key=False, null=True, blank=True)
    appeal_fax_number = models.CharField(max_length=40, null=True, blank=True)
    your_state = models.CharField(max_length=40, null=True)
    creating_professional = models.ForeignKey(
        ProfessionalUser,
        null=True,
        on_delete=models.SET_NULL,
        related_name="denials_created",
    )
    primary_professional = models.ForeignKey(
        ProfessionalUser,
        null=True,
        on_delete=models.SET_NULL,
        related_name="denials_primary",
    )
    patient_user = models.ForeignKey(PatientUser, null=True, on_delete=models.SET_NULL)
    domain = models.ForeignKey(UserDomain, null=True, on_delete=models.SET_NULL)
    patient_visible = models.BooleanField(default=True)
    professional_to_finish = models.BooleanField(default=False)

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


class ProposedAppeal(ExportModelOperationsMixin("ProposedAppeal"), models.Model):  # type: ignore
    appeal_text = models.TextField(max_length=3000000000, primary_key=False, null=True)
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
        primary_key=False,
        unique=True,
        db_index=False,
        max_length=100,
    )
    appeal_text = models.TextField(max_length=3000000000, primary_key=False, null=True)
    for_denial = models.ForeignKey(
        Denial, on_delete=models.CASCADE, null=True, blank=True
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
    # And signals on sent from fax objects
    sent = models.BooleanField(default=False)
    # Who do we want to send the appeal?
    professional_send = models.BooleanField(default=True)
    patient_send = models.BooleanField(default=True)
    patient_visible = models.BooleanField(default=True)
    pubmed_ids_json = models.CharField(max_length=600, blank=True)
    response_document_enc = EncryptedFileField(
        null=True, storage=settings.COMBINED_STORAGE
    )
    response_text = models.TextField(
        max_length=3000000000, primary_key=False, null=True
    )
    response_date = models.DateField(auto_now=False, null=True)
    mod_date = models.DateField(auto_now=True, null=True)

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
