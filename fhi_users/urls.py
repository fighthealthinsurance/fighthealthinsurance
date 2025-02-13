import typing
from django.urls import include, path
from django.conf import settings
import mfa
import mfa.TrustedDevice
from fhi_users.auth import rest_auth_views
from fhi_users.auth import auth_views
from fhi_users.auth.rest_auth_views import (
    ResendVerificationEmailView,
    RestLoginView,
    CreatePatientUserView,
    VerifyEmailView,
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
router.register(
    r"api/create_patient_user", CreatePatientUserView, basename="create_patient_user"
)
router.register(r"rest_verify_email", VerifyEmailView, basename="rest_verify_email")
router.register(
    r"resend_verification_email",
    ResendVerificationEmailView,
    basename="resend_verification_email",
)

urlpatterns = [
    path("login", auth_views.LoginView.as_view(), name="login"),
    path("logout", auth_views.LogoutView.as_view(), name="logout"),
    path("rest/router/", include(router.urls)),
    path("mfa/", include("mfa.urls")),
    path("device_add", mfa.TrustedDevice.add, name="mfa_add_new_trusted_device"),
    path(
        "verify-email-legacy/<uidb64>/<token>/",
        VerifyEmailView.as_view({"get": "retrieve"}),
        name="verify_email_legacy",
    ),
]
