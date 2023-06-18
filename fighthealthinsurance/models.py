import re
import sys
from typing import Optional

from django.db import models

from regex_field.fields import RegexField


class FollowUpType(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=300, primary_key=False, default="")
    subject = models.CharField(max_length=300, primary_key=False)
    text = models.CharField(max_length=30000, primary_key=False)
    duration = models.DurationField()

    def __str__(self):
        return self.name


class FollowUpSched(models.Model):
    follow_up_id = models.AutoField(primary_key=True)
    email = models.CharField(max_length=300, primary_key=False)
    follow_up_type = models.ForeignKey(FollowUpType, on_delete=models.CASCADE)
    follow_up_date = models.DateField(auto_now=False)
    initial = models.DateField(auto_now=False, auto_now_add=True)
    # If the denial is deleted it's either SPAM or a PII removal request
    # in either case lets delete the scheduled follow ups.
    denial_id = models.ForeignKey("Denial", on_delete=models.CASCADE)


class PlanType(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=300, primary_key=False)
    alt_name = models.CharField(max_length=300, primary_key=False)
    regex = RegexField(max_length=400, re_flags=re.IGNORECASE | re.UNICODE | re.M)
    negative_regex = RegexField(max_length=400, re_flags=re.IGNORECASE | re.UNICODE | re.M)

    def __str__(self):
        return self.name


class Regulator(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=300, primary_key=False)
    website = models.CharField(max_length=300, primary_key=False)
    alt_name = models.CharField(max_length=300, primary_key=False)
    regex = RegexField(max_length=400, re_flags=re.IGNORECASE | re.UNICODE | re.M)
    negative_regex = RegexField(max_length=400, re_flags=re.IGNORECASE | re.UNICODE | re.M)

    def __str__(self):
        return self.name


class Diagnosis(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=300, primary_key=False)
    regex = RegexField(max_length=400, re_flags=re.IGNORECASE | re.UNICODE | re.M)


class Procedures(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=300, primary_key=False)
    regex = RegexField(max_length=400, re_flags=re.IGNORECASE | re.UNICODE | re.M)


class DenialTypes(models.Model):
    id = models.AutoField(primary_key=True)
    # for the many different sub-variants.
    parent = models.ForeignKey('self', blank=True, null=True, related_name='children',
                               on_delete=models.SET_NULL)
    name = models.CharField(max_length=300, primary_key=False)
    regex = RegexField(max_length=400, re_flags=re.IGNORECASE | re.UNICODE | re.M)
    negative_regex = RegexField(max_length=400, re_flags=re.IGNORECASE | re.UNICODE | re.M)
    appeal_text = models.CharField(max_length=300, primary_key=False, blank=True)
    form = models.CharField(max_length=300, null=True)

    def get_form(self):
        if self.form is None:
            parent = self.parent
            if parent is not None:
                return parent.get_form()
            else:
                return None
        else:
            return getattr(sys.modules["fighthealthinsurance.forms"], self.form)

    def __str__(self):
        return self.name


class DataSource(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=100)


class DenialTypesRelation(models.Model):
    denial = models.ForeignKey("Denial", on_delete=models.CASCADE)
    denial_type = models.ForeignKey(DenialTypes, on_delete=models.CASCADE)
    src = models.ForeignKey(DataSource, on_delete=models.SET_NULL, null=True)


class PlanTypesRelation(models.Model):
    denial = models.ForeignKey("Denial", on_delete=models.CASCADE)
    plan_type = models.ForeignKey(PlanType, on_delete=models.CASCADE)
    src = models.ForeignKey(DataSource, on_delete=models.SET_NULL, null=True)


class Denial(models.Model):
    denial_id = models.AutoField(primary_key=True)
    hashed_email = models.CharField(max_length=300, primary_key=False)
    denial_text = models.CharField(max_length=30000000, primary_key=False)
    date = models.DateField(auto_now=False, auto_now_add=True)
    denial_type = models.ManyToManyField(DenialTypes, through=DenialTypesRelation)
    plan_type = models.ManyToManyField(PlanType, through=PlanTypesRelation)
    regulator = models.ForeignKey(Regulator, null=True, on_delete=models.SET_NULL)
    urgent = models.BooleanField(default=False)
    pre_service = models.BooleanField(default=False)
    denial_date = models.DateField(auto_now=False, null=True)
    insurance_company = models.CharField(max_length=300, primary_key=False, null=True)
    claim_id =  models.CharField(max_length=300, primary_key=False, null=True)
    procedure = models.CharField(max_length=300, primary_key=False, null=True)
    treatment = models.CharField(max_length=300, primary_key=False, null=True)
    

    def __str__(self):
        return f"{self.denial_id}: {self.denial_text[0:100]}"
