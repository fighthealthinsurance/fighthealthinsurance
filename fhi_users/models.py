import uuid
import datetime
import typing
import re
from django.db import models
from django.contrib.auth import get_user_model
from django.db.models.signals import pre_save
from django.dispatch import receiver
from enum import Enum
from loguru import logger


if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


# Define user role enum
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
        """Determine the highest role a user has"""
        if is_admin:
            return cls.ADMIN
        elif is_professional:
            return cls.PROFESSIONAL
        elif is_patient:
            return cls.PATIENT
        else:
            return cls.NONE


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
    stripe_subscription_id = models.CharField(max_length=300, null=True, blank=True)
    stripe_customer_id = models.CharField(max_length=300, null=True, blank=True)
    # Info
    # https://docs.djangoproject.com/en/5.1/ref/models/fields/#django.db.models.Field.null
    name = models.CharField(blank=True, null=True, max_length=300, unique=True)
    active = models.BooleanField()
    # Business name can be blank, we'll use display name then.
    business_name = models.CharField(max_length=300, null=True, blank=True)
    display_name = models.CharField(max_length=300, null=True, blank=True)
    professionals = models.ManyToManyField("ProfessionalUser", through="ProfessionalDomainRelation")  # type: ignore
    # The visible phone number should be unique... ish? Maybe?
    # We _could_ allow users to log in with visible phone number IFF
    # it's unique among active domains. We're going to TRY and have it
    # be unique and hope we don't have to remove this. The real world is
    # tricky.
    visible_phone_number = models.CharField(max_length=150, null=False, unique=True)
    internal_phone_number = models.CharField(
        max_length=150, null=True, unique=False, blank=True
    )
    office_fax = models.CharField(max_length=150, null=True, blank=True)
    country = models.CharField(max_length=150, default="USA")
    state = models.CharField(max_length=50, null=True, blank=True)
    city = models.CharField(max_length=150, null=True, blank=True)
    address1 = models.CharField(max_length=200, null=True, blank=True)
    address2 = models.CharField(max_length=200, null=True, blank=True)
    zipcode = models.CharField(max_length=20, null=False)
    # Customize the defaults
    default_procedure = models.CharField(
        blank=True, null=True, max_length=300, unique=False
    )
    cover_template_string = models.CharField(max_length=5000, null=True, blank=True)
    pending = models.BooleanField(default=False)
    beta = models.BooleanField(default=False)

    def save(self, *args, **kwargs):
        # Strip URL prefixes from name if it's set
        if self.name:
            self.name = self._clean_name(self.name)
        super().save(*args, **kwargs)

    @staticmethod
    def _clean_name(name: str) -> str:
        """Strip URL prefixes from name string"""
        if name:
            # Remove http://, https://, and www.
            return re.sub(r"^https?://(?:www\.)?|^www\.", "", name)
        return name

    @classmethod
    def find_by_name(cls, name: typing.Optional[str]) -> models.QuerySet["UserDomain"]:
        """Find domains by name, cleaning the input name first"""
        if name:
            cleaned_name = cls._clean_name(name)
            return cls.objects.filter(name=cleaned_name)
        return cls.objects.none()

    def get_professional_users(self, **relation_filters):
        from .models import (
            ProfessionalDomainRelation,
        )  # local import to avoid circular dependencies

        try:
            relations = ProfessionalDomainRelation.objects.filter(
                domain=self, **relation_filters
            )
            return [relation.professional for relation in relations]
        except Exception as e:
            logger.opt(exception=True).error(
                f"Error finding professional on {self} with filters {relation_filters}: {str(e)}"
            )
            raise e

    def get_address(self) -> str:
        mailing_name = self.business_name if self.business_name else self.display_name
        return f"{mailing_name}, {self.address1}, {self.address2} {self.city}, {self.state} {self.zipcode}"

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
    phone_number = models.CharField(max_length=150, null=True, blank=True)
    country = models.CharField(max_length=150, default="USA")
    state = models.CharField(max_length=50, null=True, blank=True)
    city = models.CharField(max_length=150, null=True, blank=True)
    address1 = models.CharField(max_length=200, null=True, blank=True)
    address2 = models.CharField(max_length=200, null=True, blank=True)
    zipcode = models.CharField(max_length=20, null=True, blank=True)


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
        """
        Returns a combined display and legal name for the patient user.

        If both the display name and legal name are at least two characters long and differ,
        returns the display name followed by the legal name in parentheses. If they are the same,
        returns the name. If neither is sufficiently long, returns the user's email address.
        """
        legal_name = self.get_legal_name()
        display_name = self.get_display_name()
        email = self.user.email
        if max(len(legal_name), len(display_name)) < 2:
            return email
        if legal_name == display_name:
            return legal_name
        else:
            return f"{display_name} ({legal_name})"

    def __str__(self):
        """
        Returns the combined display or legal name of the patient user as a string.
        """
        return f"{self.get_combined_name()}"


class ProfessionalUser(models.Model):
    id = models.AutoField(primary_key=True)
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    npi_number = models.CharField(blank=True, null=True, max_length=20)
    active = models.BooleanField()
    provider_type = models.CharField(blank=True, null=True, max_length=300)
    most_common_denial = models.CharField(blank=True, null=True, max_length=300)
    # Override the professional domain fax number
    fax_number = models.CharField(blank=True, null=True, max_length=40)
    domains = models.ManyToManyField("UserDomain", through="ProfessionalDomainRelation")  # type: ignore
    display_name = models.CharField(max_length=400, null=True, blank=True)
    credentials = models.CharField(max_length=400, null=True, blank=True)

    def get_display_name(self) -> str:
        if self.display_name and len(self.display_name) > 0:
            return self.display_name
        elif len(self.user.first_name) > 0:
            return f"{self.user.first_name} {self.user.last_name}"
        else:
            return self.user.email

    def admin_domains(self):
        """
        Returns a queryset of domains where the professional has an active admin relationship.
        """
        return UserDomain.objects.filter(
            professionaldomainrelation__professional=self,
            professionaldomainrelation__admin=True,
            professionaldomainrelation__active_domain_relation=True,
        )

    def get_fax_number(self):
        """
        Returns the professional's fax number, or the office fax number from the first active domain if not set.

        If neither is available, returns None.
        """
        if self.fax_number and len(self.fax_number) > 0:
            return self.fax_number
        else:
            # Return the domain fax number if available
            domains = self.domains.filter(
                professionaldomainrelation__active_domain_relation=True
            )
            if domains.exists():
                domain_opt = domains.first()
                if domain_opt:
                    return domain_opt.office_fax
            return None

    def get_full_name(self):
        """
        Returns the full legal name of the professional user as a single string.
        """
        return f"{self.user.first_name} {self.user.last_name}"

    def __str__(self):
        """
        Returns a string representation of the professional user, including full name, email,
        and, if available, fax number and NPI number.
        """
        fax_extra = ""
        fax_number = self.get_fax_number()
        if fax_number and len(fax_number) > 0:
            fax_extra = f"Professional Fax: {fax_number}"
        npi_extra = ""
        if self.npi_number and len(self.npi_number) > 0:
            npi_extra = f"NPI: {self.npi_number}"
        credentials_extra = ""
        if self.credentials and len(self.credentials) > 0:
            credentials_extra = f"Credentials: {self.credentials}"
        return f"{self.get_full_name()} ({self.user.email}, {fax_extra}, {npi_extra}, {credentials_extra})"


class ProfessionalDomainRelation(models.Model):
    professional = models.ForeignKey("ProfessionalUser", on_delete=models.CASCADE)
    domain = models.ForeignKey(UserDomain, on_delete=models.CASCADE)
    # Is the relation "active" (note: we should move this to a function)
    active_domain_relation = models.BooleanField(default=False)
    admin = models.BooleanField(default=False)
    read_only = models.BooleanField(default=False)
    professional_type = models.CharField(max_length=400, null=True, blank=True)
    pending_domain_relation = models.BooleanField(default=True)
    suspended = models.BooleanField(default=False)
    rejected = models.BooleanField(default=False)


@receiver(pre_save, sender=ProfessionalDomainRelation)
def professional_domain_relation_presave(
    sender: type, instance: ProfessionalDomainRelation, **kwargs: dict
) -> None:
    """Dynamically set the active_domain_relation field based on pending_domain_relation/suspended/rejected."""
    instance.active_domain_relation = (
        not instance.pending_domain_relation
        and not instance.suspended
        and not instance.rejected
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


class PendingProStripeCheckoutSession(models.Model):
    """
    Track Stripe checkout sessions for professional user signups and other purchases.
    This helps handle cases where users press back from Stripe checkout and retry.
    """

    id = models.AutoField(primary_key=True)
    # Stripe-specific fields
    stripe_session_id = models.CharField(max_length=255, unique=True)
    # Django session ID if available
    django_session_id = models.CharField(max_length=255, null=True, blank=True)
    # User information
    email = models.EmailField()
    visible_phone_number = models.CharField(max_length=150, null=False, blank=False)
    # Related models as foreign keys
    domain = models.ForeignKey(
        UserDomain, on_delete=models.SET_NULL, null=True, blank=True
    )
    professional_user = models.ForeignKey(
        ProfessionalUser, on_delete=models.SET_NULL, null=True, blank=True
    )
    # Tracking metadata
    created_at = models.DateTimeField(auto_now_add=True)
    completed = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)
    # Optional additional data
    metadata = models.JSONField(null=True, blank=True)

    class Meta:
        verbose_name = "Stripe Checkout Session"
        verbose_name_plural = "Stripe Checkout Sessions"
        indexes = [
            models.Index(fields=["email"]),
            models.Index(fields=["stripe_session_id"]),
            models.Index(fields=["django_session_id"]),
        ]

    def __str__(self):
        return f"Stripe Session {self.stripe_session_id} for {self.email}"
