import uuid
import time
import datetime
import typing

from django.db import models
from django.contrib.auth import get_user_model

from django.db.models.signals import pre_save
from django.dispatch import receiver


if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


# Auth-ish-related models
class UserDomain(models.Model):
    id = models.CharField(
        max_length=300,
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
        unique=True,
    )
    # Money
    stripe_subscription_id = models.CharField(max_length=300, null=True)
    # Info
    name = models.CharField(
        primary_key=False, blank=False, null=False, max_length=300, unique=True
    )
    active = models.BooleanField()
    display_name = models.CharField(max_length=300, null=False)
    # The visible phone number should be unique... ish? Maybe?
    # We _could_ allow users to log in with visible phone number IFF
    # it's unique among active domains. We're going to TRY and have it
    # be unique and hope we don't have to remove this. The real world is
    # tricky.
    visible_phone_number = models.CharField(max_length=150, null=False, unique=True)
    internal_phone_number = models.CharField(max_length=150, null=False)
    office_fax = models.CharField(max_length=150, null=True)
    country = models.CharField(max_length=150, default="USA")
    state = models.CharField(max_length=50, null=False)
    city = models.CharField(max_length=150, null=False)
    address1 = models.CharField(max_length=200, null=False)
    address2 = models.CharField(max_length=200, null=True)
    zipcode = models.CharField(max_length=20, null=False)
    # Customize the defaults
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
    active = models.BooleanField(default=False)


class ProfessionalUser(models.Model):
    id = models.AutoField(primary_key=True)
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    npi_number = models.CharField(blank=True, null=True, max_length=20)
    active = models.BooleanField()
    provider_type = models.CharField(blank=True, null=True, max_length=300)
    most_common_denial = models.CharField(blank=True, null=True, max_length=300)

    def admin_domains(self):
        return UserDomain.objects.filter(
            professionaldomainrelation__professional=self,
            professionaldomainrelation__admin=True,
            professionaldomainrelation__active=True,
        )


class ProfessionalDomainRelation(models.Model):
    professional = models.ForeignKey("ProfessionalUser", on_delete=models.CASCADE)
    domain = models.ForeignKey(UserDomain, on_delete=models.CASCADE)
    # Is the relation "active" (note: we should move this to a function)
    active = models.BooleanField(default=False)
    admin = models.BooleanField(default=False)
    read_only = models.BooleanField(default=False)
    display_name = models.CharField(max_length=400, null=True)
    professional_type = models.CharField(max_length=400, null=True)
    pending = models.BooleanField(default=True)
    suspended = models.BooleanField(default=False)
    rejected = models.BooleanField(default=False)


@receiver(pre_save, sender=ProfessionalDomainRelation)
def professional_domain_relation_presave(
    sender: type, instance: ProfessionalDomainRelation, **kwargs: dict
) -> None:
    """Dynamically set the active field based on pending/suspended/rejected."""
    instance.active = (
        not instance.pending and not instance.suspended and not instance.rejected
    )


class PatientDomainRelation(models.Model):
    patient = models.ForeignKey("PatientUser", on_delete=models.CASCADE)  # type: ignore
    domain = models.ForeignKey(UserDomain, on_delete=models.CASCADE)


class ExtraUserProperties(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    email_verified = models.BooleanField(default=False)
    # Add any other extra properties here


class VerificationToken(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    token = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    def save(self, *args, **kwargs):
        if not self.expires_at:
            if self.created_at:
                self.expires_at = self.created_at + datetime.timedelta(hours=24)
            else:
                self.expires_at = datetime.datetime.now() + datetime.timedelta(hours=24)
        super().save(*args, **kwargs)
