from django.shortcuts import render
from django.http import HttpResponseRedirect, HttpResponse
from django.urls import reverse, reverse_lazy
from django.views import generic, View
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth import get_user_model
from .auth_utils import combine_domain_and_username, resolve_domain_id
from .auth_forms import LoginForm
from django.utils.http import urlsafe_base64_decode
from django.utils.encoding import force_str
from django.contrib.auth.tokens import default_token_generator
from fhi_users.models import UserDomain
from fhi_users.audit_service import audit_service


User = get_user_model()


class LoginView(generic.FormView):
    template_name = "login.html"
    form_class = LoginForm

    def form_valid(self, form):
        """
        Handle a validated login form by authenticating the user, establishing a session, and rendering or redirecting accordingly.
        
        Processes cleaned form data (username, domain, phone, password). If neither domain nor phone is provided, or authentication fails, renders the login template with context flags indicating the failure reason. If the domain is not found, renders the login template with a domain-specific error. On successful authentication, stores the resolved domain ID in the session, logs the successful login, and redirects to the application root.
        
        Parameters:
            form (django.forms.Form): The validated login form with `username`, `domain`, `phone`, and `password` in `cleaned_data`.
        
        Returns:
            django.http.HttpResponse or django.http.HttpResponseRedirect: A rendered login page response on error, or a redirect to the root URL on successful login.
        """
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
                    # Log failed login attempt
                    audit_service.log_login_failure(
                        request,
                        attempted_email=raw_username,
                        failure_reason="invalid_credentials",
                        details={"domain_id": domain_id},
                    )
                    return render(request, "login.html", context)
                else:
                    request.session["domain_id"] = domain_id
                    login(request, user)
                    # Log successful login
                    user_domain = UserDomain.objects.filter(id=domain_id).first()
                    audit_service.log_login_success(request, user, domain=user_domain)
                    return HttpResponseRedirect(reverse("root"))
        except UserDomain.DoesNotExist:
            context["domain_invalid"] = True
            # Log failed login - domain not found
            audit_service.log_login_failure(
                request,
                attempted_email=raw_username,
                failure_reason="domain_not_found",
                details={"domain_attempted": domain, "phone_attempted": phone_number},
            )
            return render(request, "login.html", context)


class LogoutView(generic.TemplateView):
    template_name = "logout.html"

    def get(self, request, *args, **kwargs):
        # Log logout before clearing session
        """
        Handle a GET request for logout by recording the logout, ending the user's session, and clearing session cookies.
        
        The view logs the logout for authenticated users, performs Django logout, flushes server-side session data, and removes the session cookie before returning the response used to render the logout page.
        
        Returns:
            HttpResponse: Response for the logout page with the session cleared and the session cookie removed.
        """
        if request.user.is_authenticated:
            audit_service.log_logout(request, request.user)
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