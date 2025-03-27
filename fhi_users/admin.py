from django.contrib import admin
from fhi_users.models import (
    UserDomain,
    GlobalUserRelation,
    UserContactInfo,
    PatientUser,
    ProfessionalUser,
    ProfessionalDomainRelation,
    PatientDomainRelation,
    ExtraUserProperties,
    VerificationToken,
    ResetToken,
)

@admin.register(GlobalUserRelation)
class GlobalUserRelationAdmin(admin.ModelAdmin):
    list_display = ("id", "parent_user", "child_user")


@admin.register(UserContactInfo)
class UserContactInfoAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "phone_number", "country", "state", "city", "zipcode")
    search_fields = ("user__username", "phone_number", "city")
    list_filter = ("country", "state")


@admin.register(ProfessionalDomainRelation)
class ProfessionalDomainRelationAdmin(admin.ModelAdmin):
    list_display = ("id", "professional", "domain", "active", "admin", "read_only")
    search_fields = ("professional__user__username", "domain__name")
    list_filter = ("active", "admin", "read_only", "pending", "suspended", "rejected")


@admin.register(PatientDomainRelation)
class PatientDomainRelationAdmin(admin.ModelAdmin):
    list_display = ("id", "patient", "domain")


@admin.register(ExtraUserProperties)
class ExtraUserPropertiesAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "email_verified")
    list_filter = ("email_verified",)


@admin.register(VerificationToken)
class VerificationTokenAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "token", "created_at", "expires_at")
    search_fields = ("user__username", "token")
    list_filter = ("created_at", "expires_at")


@admin.register(ResetToken)
class ResetTokenAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "token", "created_at", "expires_at")
    search_fields = ("user__username", "token")
    list_filter = ("created_at", "expires_at")
