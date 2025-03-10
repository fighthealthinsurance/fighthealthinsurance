import uuid
import time
import datetime
import typing
import re
from enum import Enum

from django.db import models
from django.contrib.auth import get_user_model
from django.db.models.signals import pre_save
from django.dispatch import receiver

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


class UserRole(str, Enum):
    """
    Enum representing possible user roles in the system, in order of increasing permissions.
    """
    NONE = "none"
    PATIENT = "patient"
    PROFESSIONAL = "professional"
    ADMIN = "admin"

    @classmethod
    def get_highest_role(cls, is_patient, is_professional, is_admin):
        """Determine the highest role a user has."""
        if is_admin:
            return cls.ADMIN
        elif is_professional:
            return cls.PROFESSIONAL
        elif is_patient:
            return cls.PATIENT
        else:
            return cls.NONE


class UserDomain(models.Model):
    """
    Domain model representing a user domain.
    
    The 'internal_phone_number' field is not unique and caution should be taken when
    using it for lookups.
    """
    id = models.CharField(
        max_length=300,
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
        unique=True,
    )
    stripe_subscription_id = models.CharField(max_length=300, null=True)
    name = models.CharField(blank=True, null=True, max_length=300, unique=True)
    active = models.BooleanField()
    business_name = models.CharField(max_length=300, null=True)
    display_name = models.CharField(max_length=300, null=False)
    professionals = models.ManyToManyField("ProfessionalUser", through="ProfessionalDomainRelation")  # type: ignore
    visible_phone_number = models.CharField(max_length=150, null=False, unique=True)
    internal_phone_number = models.CharField(max_length=150, null=True, blank=True)
    office_fax = models.CharField(max_length=150, null=True, blank=True)
    country = models.CharField(max_length=150, default="USA")
    state = models.CharField(max_length=50, null=False)
    city = models.CharField(max_length=150, null=False)
    address1 = models.CharField(max_length=200, null=False)
    address2 = models.CharField(max_length=200, null=True, blank=True)
    zipcode = models.CharField(max_length=20, null=False)
    default_procedure = models.CharField(blank=False, null=True, max_length=300, unique=False)
    cover_template_string = models.CharField(max_length=5000, null=True)

    @staticmethod
    def _clean_name(name: str) -> str:
        """Strip URL prefixes from name string."""
        if name:
            # Remove http://, https://, and www.
            return re.sub(r"^https?://(?:www\.)?|^www\.", "", name)
        return name

    @classmethod
    def find_by_name(cls, name: typing.Optional[str]) -> models.QuerySet["UserDomain"]:
        """
        Find domains by name, cleaning the input name first.
        """
        if name:
            cleaned_name = cls._clean_name(name)
            return cls.objects.filter(name=cleaned_name)
        return cls.objects.none()

    def get_professional_users(self, **relation_filters):
        from .models import ProfessionalDomainRelation  # local import to avoid circular dependencies
        relations = ProfessionalDomainRelation.objects.filter(domain=self, **relation_filters)
        return [relation.professional for relation in relations]


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
    display_name = models.CharField(max_length=300, null=True)

    def get_display_name(self) -> str:
        if self.display_name and len(self.display_name) > 1:
            return self.display_name
        else:
            return self.get_legal_name()

    def get_legal_name(self) -> str:
        return f"{self.user.first_name} {self.user.last_name}"

    def get_combined_name(self) -> str:
        legal_name = self.get_legal_name()
        display_name = self.get_display_name()
        email = self.user.email
        if max(len(legal_name), len(display_name)) < 2:
            return email
        if legal_name == display_name:
            return legal_name
        else:
            return f"{display_name} ({legal_name})"


class ProfessionalUser(models.Model):
    id = models.AutoField(primary_key=True)
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    npi_number = models.CharField(blank=True, null=True, max_length=20)
    active = models.BooleanField()
    provider_type = models.CharField(blank=True, null=True, max_length=300)
    most_common_denial = models.CharField(blank=True, null=True, max_length=300)
    fax_number = models.CharField(blank=True, null=True, max_length=40)
    domains = models.ManyToManyField("UserDomain", through="ProfessionalDomainRelation")  # type: ignore
    display_name = models.CharField(max_length=400, null=True)

    def get_display_name(self) -> str:
        if self.display_name and len(self.display_name) > 0:
            return self.display_name
        elif len(self.user.first_name) > 0:
            return f"{self.user.first_name} {self.user.last_name}"
        else:
            return self.user.email

    def admin_domains(self):
        return UserDomain.objects.filter(
            professionaldomainrelation__professional=self,
            professionaldomainrelation__admin=True,
            professionaldomainrelation__active=True,
        )

    def get_full_name(self):
        return f"{self.user.first_name} {self.user.last_name}"


class ProfessionalDomainRelation(models.Model):
    professional = models.ForeignKey("ProfessionalUser", on_delete=models.CASCADE)
    domain = models.ForeignKey(UserDomain, on_delete=models.CASCADE)
    active = models.BooleanField(default=False)
    admin = models.BooleanField(default=False)
    read_only = models.BooleanField(default=False)
    professional_type = models.CharField(max_length=400, null=True)
    pending = models.BooleanField(default=True)
    suspended = models.BooleanField(default=False)
    rejected = models.BooleanField(default=False)


@receiver(pre_save, sender=ProfessionalDomainRelation)
def professional_domain_relation_presave(sender: type, instance: ProfessionalDomainRelation, **kwargs: dict) -> None:
    """
    Dynamically set the 'active' field based on pending, suspended, and rejected statuses.
    """
    instance.active = not (instance.pending or instance.suspended or instance.rejected)


class PatientDomainRelation(models.Model):
    patient = models.ForeignKey("PatientUser", on_delete=models.CASCADE)  # type: ignore
    domain = models.ForeignKey(UserDomain, on_delete=models.CASCADE)


class ExtraUserProperties(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    email_verified = models.BooleanField(default=False)
    # Additional extra properties can be added here.


class VerificationToken(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    token = models.CharField(max_length=255, default=uuid.uuid4)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    def save(self, *args, **kwargs):
        if not self.expires_at:
            if self.created_at:
                self.expires_at = self.created_at + datetime.timedelta(hours=24)
            else:
                self.expires_at = datetime.datetime.now() + datetime.timedelta(hours=24)
        super().save(*args, **kwargs)


class ResetToken(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    token = models.CharField(max_length=255, default=uuid.uuid4)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    def save(self, *args, **kwargs):
        if not self.expires_at:
            if self.created_at:
                self.expires_at = self.created_at + datetime.timedelta(hours=24)
            else:
                self.expires_at = datetime.datetime.now() + datetime.timedelta(hours=24)
        super().save(*args, **kwargs)
