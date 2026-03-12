from django.conf import settings
from django.shortcuts import render
from django.http import HttpResponseRedirect, HttpResponse
from django.urls import reverse, reverse_lazy
from django.utils.http import urlsafe_base64_decode
from django.views import generic, View
from django.contrib.auth import authenticate, login, logout, REDIRECT_FIELD_NAME
from django.contrib.auth import get_user_model
from django.contrib.auth.views import RedirectURLMixin
from .auth_utils import combine_domain_and_username, resolve_domain_id
from .auth_forms import LoginForm
from django.utils.encoding import force_str
from django.contrib.auth.tokens import default_token_generator
from fhi_users.models import UserDomain

User = get_user_model()


class LoginView(RedirectURLMixin, generic.FormView):
    template_name = "login.html"
    form_class = LoginForm
    next_page = reverse_lazy("root")

    def get_success_url_allowed_hosts(self):
        hosts = super().get_success_url_allowed_hosts()
        for host in getattr(settings, "ALLOWED_HOSTS", []):
            if host and host != "*" and not host.startswith("."):
                hosts.add(host)
        return hosts

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context[REDIRECT_FIELD_NAME] = self.request.GET.get(REDIRECT_FIELD_NAME, "")
        return context

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
                    return HttpResponseRedirect(self.get_success_url())
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
