from django.db import models
from django.contrib.auth import get_user_model

User = get_user_model()


# Auth-ish-related models
class UserDomain(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.CharField(
        primary_key=False, blank=False, null=False, max_length=300, unique=True
    )
    active = models.BooleanField()
    display_name = models.CharField(max_length=300, null=True)
    visible_phone_number = models.CharField(max_length=150, null=True)
    internal_phone_number = models.CharField(max_length=150, null=True)
    office_fax = models.CharField(max_length=150, null=True)
    country = models.CharField(max_length=150, default="USA")
    state = models.CharField(max_length=50, null=True)
    city = models.CharField(max_length=150, null=True)
    address1 = models.CharField(max_length=200, null=True)
    address2 = models.CharField(max_length=200, null=True)
    zipcode = models.CharField(max_length=20, null=True)
    # Some extra defaults
    default_procedure = models.CharField(
        primary_key=False, blank=False, null=True, max_length=300, unique=False
    )
    # Maybe include:
    # List of common procedures
    # Common appeal templates
    # Extra model prompt


# As its set up a user can be in multiple domains & pro & patient
# however (for now) the usernames & domains are scoped so that we can
# allow admin to reset passwords within the domain. But we can later
# add "global" users that aggregate multiple sub-users. Maybe. idk
class GlobalUserRelation(models.Model):
    id = models.AutoField(primary_key=True)
    parent_user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="%(class)s_parent_user"
    )
    child_user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="%(class)s_child_user"
    )


class UserContactInfo(models.Model):
    id = models.AutoField(primary_key=True)
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    phone_number = models.CharField(max_length=150, null=True)
    country = models.CharField(max_length=150, default="USA")
    state = models.CharField(max_length=50, null=True)
    city = models.CharField(max_length=150, null=True)
    address1 = models.CharField(max_length=200, null=True)
    address2 = models.CharField(max_length=200, null=True)
    zipcode = models.CharField(max_length=20, null=True)


class PatientUser(models.Model):
    id = models.AutoField(primary_key=True)
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    active = models.BooleanField()


class ProfessionalUser(models.Model):
    id = models.AutoField(primary_key=True)
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    npi_number = models.CharField(blank=True, null=True, max_length=20)
    active = models.BooleanField()
    most_common_denial = models.CharField(blank=True, null=True, max_length=300)


class ProfessionalDomainRelation(models.Model):
    professional = models.ForeignKey("ProfessionalUser", on_delete=models.CASCADE)
    domain = models.ForeignKey(UserDomain, on_delete=models.CASCADE)
    active = models.BooleanField()
    admin = models.BooleanField()
    read_only = models.BooleanField(default=False)
    approval_required = models.BooleanField(default=True)
    display_name = models.CharField(max_length=400, null=True)
    professional_type = models.CharField(max_length=400, null=True)
    pending = models.BooleanField(
        default=True
    )  # Has an admin accepted the request to join the domain?


class PatientDomainRelation(models.Model):
    patient = models.ForeignKey("PatientUser", on_delete=models.CASCADE)
    domain = models.ForeignKey(UserDomain, on_delete=models.CASCADE)
    active = models.BooleanField(default=True)
