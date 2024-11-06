import hashlib
import os
import re
import sys
import tempfile
import uuid

from django.conf import settings
from django.db import models
from django.db.models.functions import Now

from fighthealthinsurance.utils import sekret_gen
from regex_field.fields import RegexField


class InterestedProfessional(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=300, primary_key=False, default="")
    business_name = models.CharField(max_length=300, primary_key=False, default="")
    phone_number = models.CharField(max_length=300, primary_key=False, default="")
    address = models.CharField(max_length=1000, primary_key=False, default="")
    email = models.EmailField()
    comments = models.TextField(primary_key=False, default="")
    most_common_denial = models.CharField(max_length=300, default="")
    job_title_or_provider_type = models.CharField(max_length=300, default="")
    paid = models.BooleanField(default=False)
    clicked_for_paid = models.BooleanField(default=True)
    signup_date = models.DateField(auto_now=True)


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
        return f"{self.email} on {self.follow_up_date} for {self.denial_id}"


class PlanType(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=300, primary_key=False)
    alt_name = models.CharField(max_length=300, primary_key=False)
    regex = RegexField(max_length=400, re_flags=re.IGNORECASE | re.UNICODE | re.M)
    negative_regex = RegexField(
        max_length=400, re_flags=re.IGNORECASE | re.UNICODE | re.M
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
        return "{self.id}:{self.name}"


class Procedures(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=300, primary_key=False)
    regex = RegexField(max_length=400, re_flags=re.IGNORECASE | re.UNICODE | re.M)

    def __str__(self):
        return "{self.id}:{self.name}"


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
        max_length=400, re_flags=re.IGNORECASE | re.UNICODE | re.M, null=True
    )
    procedure_regex = RegexField(
        max_length=400, re_flags=re.IGNORECASE | re.UNICODE | re.M, null=True
    )
    negative_regex = RegexField(
        max_length=400, re_flags=re.IGNORECASE | re.UNICODE | re.M
    )
    appeal_text = models.TextField(max_length=3000, primary_key=False, blank=True)
    form = models.CharField(max_length=300, null=True)

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
                    sys.modules["fighthealthinsurance.question_forms"], self.form
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
        return "{self.id}:{self.name}"


class DataSource(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=100)

    def __str__(self):
        return "{self.id}:{self.name}"


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


class PlanDocuments(models.Model):
    plan_document_id = models.AutoField(primary_key=True)
    plan_document = models.FileField(null=True, storage=settings.COMBINED_STORAGE)
    # If the denial is deleted it's either SPAM or a removal request in either case
    # we cascade the delete
    denial = models.ForeignKey("Denial", on_delete=models.CASCADE)


class FollowUpDocuments(models.Model):
    document_id = models.AutoField(primary_key=True)
    follow_up_document = models.FileField(null=True, storage=settings.COMBINED_STORAGE)
    # If the denial is deleted it's either SPAM or a removal request in either case
    # we cascade the delete
    denial = models.ForeignKey("Denial", on_delete=models.CASCADE)
    follow_up_id = models.ForeignKey("FollowUp", on_delete=models.CASCADE, null=True)


class PubMedArticleSummarized(models.Model):
    """PubMedArticles with a summary for the given query."""

    pmid = models.TextField(primary_key=False, blank=True)
    doi = models.TextField(primary_key=False, blank=True)
    query = models.TextField(primary_key=False, blank=True)
    title = models.TextField(blank=True, null=True)
    abstract = models.TextField(primary_key=False, blank=True, null=True)
    text = models.TextField(primary_key=False, blank=True, null=True)
    basic_summary = models.TextField(primary_key=False, blank=True, null=True)
    says_effective = models.BooleanField(null=True)
    publication_date = models.DateTimeField(null=True)
    retrival_date = models.TextField(blank=True, null=True)


class PubMedQueryData(models.Model):
    internal_id = models.AutoField(primary_key=True)
    query = models.TextField(null=False, max_length=300)
    articles = models.TextField(null=True)  # json
    query_date = models.DateTimeField(auto_now_add=True)
    denial_id = models.ForeignKey("Denial", on_delete=models.SET_NULL, null=True)


class FaxesToSend(models.Model):
    fax_id = models.AutoField(primary_key=True)
    hashed_email = models.CharField(max_length=300, primary_key=False)
    date = models.DateTimeField(auto_now=False, auto_now_add=True)
    paid = models.BooleanField()
    email = models.CharField(max_length=300)
    name = models.CharField(max_length=300, null=True)
    appeal_text = models.TextField()
    pmids = models.CharField(max_length=300)
    health_history = models.TextField(null=True)
    combined_document = models.FileField(null=True, storage=settings.COMBINED_STORAGE)
    uuid = models.CharField(
        max_length=300, primary_key=False, default=uuid.uuid4, editable=False
    )
    sent = models.BooleanField(default=False)
    attempting_to_send_as_of = models.DateField(
        auto_now=False, auto_now_add=False, null=True
    )
    denial_id = models.ForeignKey("Denial", on_delete=models.CASCADE, null=True)
    destination = models.CharField(max_length=20, null=True)
    should_send = models.BooleanField(default=False)

    def get_temporary_document_path(self):
        with tempfile.NamedTemporaryFile(
            suffix=self.combined_document.name, mode="w+b", delete=False
        ) as f:
            f.write(self.combined_document.read())
            f.flush()
            f.close()
            os.sync()
            print(f"Constructed temp path {f.name}")
            return f.name


class Denial(models.Model):
    denial_id = models.AutoField(primary_key=True, null=False)
    uuid = models.CharField(
        max_length=300, primary_key=False, default=uuid.uuid4, editable=False
    )
    hashed_email = models.CharField(max_length=300, primary_key=False)
    denial_text = models.TextField(primary_key=False)
    denial_type_text = models.TextField(max_length=200, primary_key=False, null=True)
    date = models.DateField(auto_now=False, auto_now_add=True)
    denial_type = models.ManyToManyField(DenialTypes, through=DenialTypesRelation)
    plan_type = models.ManyToManyField(PlanType, through=PlanTypesRelation)
    plan_source = models.ManyToManyField(PlanSource, through=PlanSourceRelation)
    employer_name = models.CharField(max_length=300, null=True)
    regulator = models.ForeignKey(Regulator, null=True, on_delete=models.SET_NULL)
    urgent = models.BooleanField(default=False)
    pre_service = models.BooleanField(default=False)
    denial_date = models.DateField(auto_now=False, null=True)
    insurance_company = models.CharField(max_length=300, primary_key=False, null=True)
    claim_id = models.CharField(max_length=300, primary_key=False, null=True)
    procedure = models.CharField(max_length=300, primary_key=False, null=True)
    diagnosis = models.CharField(max_length=300, primary_key=False, null=True)
    appeal_text = models.TextField(primary_key=False, null=True)
    raw_email = models.TextField(max_length=300, primary_key=False, null=True)
    created = models.DateTimeField(db_default=Now(), primary_key=False, null=True)
    use_external = models.BooleanField(default=False)
    health_history = models.TextField(primary_key=False, null=True)
    qa_context = models.TextField(primary_key=False, null=True)
    plan_context = models.TextField(primary_key=False, null=True)
    semi_sekret = models.CharField(max_length=100, default=sekret_gen)
    plan_id = models.CharField(max_length=200, primary_key=False, null=True)
    state = models.CharField(max_length=4, primary_key=False, null=True)
    appeal_result = models.CharField(max_length=200, null=True)
    last_interaction = models.DateTimeField(auto_now=True)
    follow_up_semi_sekret = models.CharField(max_length=100, default=sekret_gen)
    references = models.TextField(primary_key=False, null=True)
    reference_summary = models.TextField(primary_key=False, null=True)
    appeal_fax_number = models.CharField(max_length=12, null=True)

    def follow_up(self):
        return self.raw_email is not None and "@" in self.raw_email

    def chose_appeal(self):
        return self.appeal_text is not None and len(self.appeal_text) > 10

    def __str__(self):
        return f"{self.denial_id} -- {self.date} -- Follow Up: {self.follow_up()} -- Chose Appeal {self.chose_appeal()}"

    @staticmethod
    def get_hashed_email(email):
        encoded_email = email.encode("utf-8").lower()
        return hashlib.sha512(encoded_email).hexdigest()


class ProposedAppeal(models.Model):
    appeal_text = models.TextField(max_length=3000000000, primary_key=False, null=True)
    for_denial = models.ForeignKey(
        Denial, on_delete=models.CASCADE, null=True, blank=True
    )
    chosen = models.BooleanField(default=False)
    editted = models.BooleanField(default=False)

    def __str__(self):
        if self.appeal_text is not None:
            return f"{self.for_denial}: {self.appeal_text[0:100]}"
        else:
            return f"{self.for_denial}: {self.appeal_text}"
