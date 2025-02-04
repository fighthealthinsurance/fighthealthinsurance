from django.contrib.auth.models import AbstractUser
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
    # Some extra defaults
    default_procedure = models.CharField(
        primary_key=False, blank=False, null=True, max_length=300, unique=False
    )
    # Maybe include:
    # List of common procedures
    # Common appeal templates
    # Extra model prompt


# As its set up a user can be in multiple domains & pro & consumer
# however (for now) the usernames & domains are scoped so that we can
# allow admin to reset passwords within the domain. But we can later
# add "global" users that aggregate multiple sub-users. Maybe. idk
class GlobalUserRelation(models.Model):
    id = models.AutoField(primary_key=True)
    parent_user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='%(class)s_parent_user')
    child_user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='%(class)s_child_user')

class ConsumerUser(models.Model):
    id = models.AutoField(primary_key=True)
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    active = models.BooleanField()


class ProfessionalUser(models.Model):
    id = models.AutoField(primary_key=True)
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    npi_number = models.CharField(blank=True, null=True, max_length=20)
    active = models.BooleanField()


class ProfessionalDomainRelation(models.Model):
    professional = models.ForeignKey("ProfessionalUser", on_delete=models.CASCADE)
    domain = models.ForeignKey(UserDomain, on_delete=models.CASCADE)
    active = models.BooleanField()
    admin = models.BooleanField()
    read_only = models.BooleanField(default=False)
    approval_required = models.BooleanField(default=True)


class ConsumerDomainRelation(models.Model):
    consumer = models.ForeignKey("ConsumerUser", on_delete=models.CASCADE)
    domain = models.ForeignKey(UserDomain, on_delete=models.CASCADE)
    active = models.BooleanField()
