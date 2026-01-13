from django.shortcuts import render
from django.http import HttpResponseRedirect, HttpResponse
from django.urls import reverse, reverse_lazy
from django.views import generic, View
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth import get_user_model
from .auth_utils import combine_domain_and_username, resolve_domain_id
from .auth_forms import LoginForm, PatientSignupForm
from django.utils.http import urlsafe_base64_decode
from django.utils.encoding import force_str
from django.contrib.auth.tokens import default_token_generator
from fhi_users.models import UserDomain


User = get_user_model()


class LoginView(generic.FormView):
    template_name = "login.html"
    form_class = LoginForm

    def form_valid(self, form):
        context: dict[str, bool] = {}
        raw_username = form.cleaned_data["username"]
        request = self.request
        domain = form.cleaned_data["domain"]
        phone_number = form.cleaned_data["phone"]
        password = form.cleaned_data["password"]
        try:
            if not domain and not phone_number:
                context["invalid"] = True
                context["need_phone_or_domain"] = True
            else:
                domain_id = resolve_domain_id(
                    domain_name=domain, phone_number=phone_number
                )
                username = combine_domain_and_username(
                    raw_username, domain_id=domain_id
                )
                user = authenticate(username=username, password=password)
                if user is None:
                    context["invalid"] = True
                    context["bad_credentials"] = True
                    return render(request, "login.html", context)
                else:
                    request.session["domain_id"] = domain_id
                    login(request, user)
                    return HttpResponseRedirect(reverse("root"))
        except UserDomain.DoesNotExist:
            context["domain_invalid"] = True
            return render(request, "login.html", context)


class LogoutView(generic.TemplateView):
    template_name = "logout.html"

    def get(self, request, *args, **kwargs):
        logout(request)
        response = super().get(request, *args, **kwargs)
        # Clear session cookies
        request.session.flush()
        response.delete_cookie("sessionid")
        return response


class VerifyEmailView(View):
    def get(self, request, uidb64, token):
        try:
            uid = force_str(urlsafe_base64_decode(uidb64))
            user = User.objects.get(pk=uid)
        except (TypeError, ValueError, OverflowError, User.DoesNotExist):
            user = None
        if user is not None and default_token_generator.check_token(user, token):
            user.is_active = True
            user.extrauserproperties.email_verified = True
            user.save()
            return HttpResponseRedirect(reverse_lazy("login"))
        else:
            return HttpResponse("Activation link is invalid!")


class PatientSignupView(generic.FormView):
    """Simple signup view for patient users."""

    template_name = "signup.html"
    form_class = PatientSignupForm

    def form_valid(self, form):
        from fhi_users.models import PatientUser

        email = form.cleaned_data["email"]
        password = form.cleaned_data["password"]
        first_name = form.cleaned_data.get("first_name", "")
        last_name = form.cleaned_data.get("last_name", "")

        # Create user with email as username
        user = User.objects.create_user(
            username=email,
            email=email,
            password=password,
            first_name=first_name,
            last_name=last_name,
            is_active=True,
        )

        # Create associated PatientUser
        patient_user = PatientUser.objects.create(user=user, active=True)

        # Link anonymous appeals to the new account if emails match
        try:
            from fighthealthinsurance.models import Denial
            from fighthealthinsurance.utils import hash_for_link
            import logging

            logger = logging.getLogger(__name__)

            hashed = hash_for_link(email)
            anonymous_denials = Denial.objects.filter(
                hashed_email=hashed, patient_user__isnull=True
            )
            if anonymous_denials.exists():
                count = anonymous_denials.update(patient_user=patient_user)
                logger.info(
                    f"Migrated {count} anonymous denials to patient user {patient_user.id} "
                    f"for email {email}"
                )
        except Exception as e:
            # Log but don't fail signup if migration fails
            import logging

            logger = logging.getLogger(__name__)
            logger.error(
                f"Failed to migrate anonymous denials for {email}: {e}", exc_info=True
            )

        # Log the user in
        login(self.request, user)

        # Redirect to dashboard
        return HttpResponseRedirect(reverse("patient-dashboard"))
