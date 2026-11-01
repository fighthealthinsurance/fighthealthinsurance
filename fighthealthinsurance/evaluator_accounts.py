import datetime
from typing import Any

from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model, login, password_validation
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.utils.decorators import method_decorator
from django.views import generic
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters

EVALUATOR_GROUP_NAME = "evaluators"
User = get_user_model()


def is_evaluator(user: Any) -> bool:
    """An active, non-admin account explicitly marked for Arena evaluation."""
    return bool(
        getattr(user, "is_authenticated", False)
        and user.is_active
        and not user.is_staff
        and not user.is_superuser
        and user.groups.filter(name=EVALUATOR_GROUP_NAME).exists()
    )


EVALUATOR_LOGIN_WINDOW = datetime.timedelta(minutes=15)
EVALUATOR_LOGIN_ACCOUNT_LIMIT = 5
EVALUATOR_LOGIN_ADDRESS_LIMIT = 30
EVALUATOR_LOGIN_THROTTLED_MESSAGE = (
    "Too many failed login attempts. Please try again in 15 minutes."
)


def _login_throttle_buckets(request, username: str):
    from fighthealthinsurance import client_network

    normalized = username.strip().lower()
    account_key = salted_hmac("evaluator-login-account", normalized).hexdigest()
    raw_address = client_network.cf_ip_from_meta(request.META) or request.META.get(
        "REMOTE_ADDR", ""
    )
    network = client_network.prefix_of(raw_address, v4_bits=32, v6_bits=64)
    address_key = client_network.period_key(b"evaluator-login-address", "all", network)
    return (
        (account_key, EVALUATOR_LOGIN_ACCOUNT_LIMIT),
        (address_key, EVALUATOR_LOGIN_ADDRESS_LIMIT),
    )


def _login_attempt_allowed(buckets) -> bool:
    from fighthealthinsurance.models import EvaluatorLoginThrottle

    cutoff = timezone.now() - EVALUATOR_LOGIN_WINDOW
    for digest, limit in buckets:
        failures = (
            EvaluatorLoginThrottle.objects.filter(
                digest=digest, window_started_at__gte=cutoff
            )
            .values_list("failures", flat=True)
            .first()
        )
        if failures is not None and failures >= limit:
            return False
    return True


def _record_login_failure(buckets) -> bool:
    from fighthealthinsurance.models import EvaluatorLoginThrottle

    now = timezone.now()
    cutoff = now - EVALUATOR_LOGIN_WINDOW
    allowed = True
    for digest, limit in buckets:
        with transaction.atomic():
            row, _ = EvaluatorLoginThrottle.objects.get_or_create(
                digest=digest,
                defaults={"window_started_at": now, "last_failure_at": now},
            )
            row = EvaluatorLoginThrottle.objects.select_for_update().get(pk=row.pk)
            if row.window_started_at < cutoff:
                row.failures = 0
                row.window_started_at = now
            row.failures += 1
            row.last_failure_at = now
            row.save(update_fields=["failures", "window_started_at", "last_failure_at"])
            if row.failures >= limit:
                allowed = False
    return allowed


def _clear_account_login_failures(buckets) -> None:
    from fighthealthinsurance.models import EvaluatorLoginThrottle

    EvaluatorLoginThrottle.objects.filter(digest=buckets[0][0]).delete()


class EvaluatorAccountForm(forms.Form):
    email = forms.EmailField()
    password1 = forms.CharField(
        label="Password",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    password2 = forms.CharField(
        label="Confirm password",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )

    def clean_email(self) -> str:
        email = self.cleaned_data["email"].strip().lower()
        if len(email) > 150:
            raise ValidationError("Use an email address of 150 characters or fewer.")
        if (
            User.objects.filter(username__iexact=email).exists()
            or User.objects.filter(email__iexact=email).exists()
        ):
            raise ValidationError("An account with this email already exists.")
        return str(email)

    def clean_password2(self) -> str:
        password1 = self.cleaned_data.get("password1")
        password2 = self.cleaned_data["password2"]
        if password1 != password2:
            raise ValidationError("The passwords do not match.")
        email = self.cleaned_data.get("email", "")
        password_validation.validate_password(
            password2, user=User(username=email, email=email)
        )
        return str(password2)

    @transaction.atomic
    def save(self):
        email = self.cleaned_data["email"]
        user = User.objects.create_user(
            username=email,
            email=email,
            password=self.cleaned_data["password2"],
            is_active=True,
            is_staff=False,
            is_superuser=False,
        )
        user.user_permissions.clear()
        group, _ = Group.objects.get_or_create(name=EVALUATOR_GROUP_NAME)
        user.groups.set([group])
        return user


class EvaluatorLoginForm(AuthenticationForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].label = "Email address"

    def clean_username(self) -> str:
        return str(self.cleaned_data["username"].strip().lower())

    def confirm_login_allowed(self, user) -> None:
        super().confirm_login_allowed(user)
        if not is_evaluator(user):
            raise ValidationError(
                self.error_messages["invalid_login"],
                code="invalid_login",
                params={"username": self.username_field.verbose_name},
            )


@method_decorator(sensitive_post_parameters("password1", "password2"), name="dispatch")
class AddEvaluatorAccountView(generic.FormView):
    template_name = "add_evaluator_account.html"
    form_class = EvaluatorAccountForm

    def get_success_url(self) -> str:
        return reverse("staff_dashboard")

    def form_valid(self, form):
        try:
            user = form.save()
        except IntegrityError:
            form.add_error("email", "An account with this email already exists.")
            return self.form_invalid(form)
        messages.success(
            self.request,
            f"Evaluator account created for {user.email}. "
            f"Login page: {reverse('evaluator_login')}",
        )
        return super().form_valid(form)


@method_decorator([never_cache, sensitive_post_parameters("password")], name="dispatch")
class EvaluatorLoginView(generic.FormView):
    template_name = "evaluator_login.html"
    form_class = EvaluatorLoginForm

    def dispatch(self, request, *args, **kwargs):
        if is_evaluator(request.user):
            return redirect("chooser")
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["request"] = self.request
        return kwargs

    def post(self, request, *args, **kwargs):
        self._throttle_buckets = _login_throttle_buckets(
            request, request.POST.get("username", "")
        )
        if not _login_attempt_allowed(self._throttle_buckets):
            return self._throttled_response()
        return super().post(request, *args, **kwargs)

    def _throttled_response(self):
        form = self.form_class(request=self.request)
        return self.render_to_response(
            self.get_context_data(
                form=form,
                login_throttled=True,
                login_throttled_message=EVALUATOR_LOGIN_THROTTLED_MESSAGE,
            ),
            status=429,
        )

    def form_invalid(self, form):
        if self.request.POST.get("username") and self.request.POST.get("password"):
            if not _record_login_failure(self._throttle_buckets):
                return self._throttled_response()
        return super().form_invalid(form)

    def form_valid(self, form):
        _clear_account_login_failures(self._throttle_buckets)
        login(self.request, form.get_user())
        return redirect("chooser")
