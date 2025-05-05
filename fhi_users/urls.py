import typing
from django.urls import include, path
from django.conf import settings
import mfa
import mfa.TrustedDevice
from fhi_users.auth import rest_auth_views
from fhi_users.auth import auth_views
from fhi_users.auth.rest_auth_views import (
    RestLoginView,
    PatientUserViewSet,
    VerifyEmailViewSet,
    PasswordResetViewSet,
    UserDomainViewSet,
    ProfessionalUserUpdateViewSet,
    PasswordViewSet,
)
from rest_framework import routers

if settings.DEBUG:
    RouterClass: typing.Type[routers.BaseRouter] = routers.DefaultRouter
else:
    RouterClass = routers.SimpleRouter

router = RouterClass()
router.register(
    r"professional_user",
    rest_auth_views.ProfessionalUserViewSet,
    basename="professional_user",
)
router.register(r"api/login", RestLoginView, basename="rest_login")
router.register(r"api/patient_user", PatientUserViewSet, basename="patient_user")
router.register(r"rest_verify_email", VerifyEmailViewSet, basename="rest_verify_email")
router.register(r"password_reset", PasswordResetViewSet, basename="password_reset")
router.register(r"whoami", rest_auth_views.WhoAmIViewSet, basename="whoami")
router.register(
    r"domain_exists", rest_auth_views.UserDomainExistsViewSet, basename="domain_exists"
)
router.register(r"user_domain", UserDomainViewSet, basename="user_domain")
router.register(
    r"professional_profile",
    ProfessionalUserUpdateViewSet,
    basename="professional_profile",
)
router.register(r"password", PasswordViewSet, basename="password")

urlpatterns = [
    # Rest APIs served under here
    path("rest/router/", include(router.urls)),
    # Non-rest views
    path("login", auth_views.LoginView.as_view(), name="login"),
    path("logout", auth_views.LogoutView.as_view(), name="logout"),
    path("mfa/", include("mfa.urls")),
    path("device_add", mfa.TrustedDevice.add, name="mfa_add_new_trusted_device"),
    path(
        "verify-email-legacy/<uidb64>/<token>/",
        auth_views.VerifyEmailView.as_view(),
        name="verify_email_legacy",
    ),
]
