from django.shortcuts import render
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.views import generic
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth import get_user_model
from .auth_utils import validate_username, combine_domain_and_username
from .auth_forms import DomainAuthenticationForm
import fhi_users

User = get_user_model()


class LoginView(generic.FormView):
    template_name = "login.html"
    form_class = DomainAuthenticationForm

    def form_valid(self, form):
        context = {}
        raw_username = form.cleaned_data["username"]
        request = self.request
        domain = form.cleaned_data["domain"]
        try:
            username = combine_domain_and_username(raw_username, domain)
        except fhi_users.models.UserDomain.DoesNotExist:
            context["domain_invalid"] = True
            return render(request, "login.html", context)
        password = form.cleaned_data["password"]
        print(f"Performing auth for {username} w/ {password}")
        user = authenticate(username=username, password=password)
        if user:
            from mfa.helpers import has_mfa

            res = has_mfa(
                username=username, request=request
            )  # has_mfa returns false or HttpResponseRedirect
            if res:
                return res
            return create_session(request, user.username)
        context["invalid"] = True
        return render(request, "login.html", context)


def create_session(request, username):
    user = User.objects.get(username=username)
    login(request, user)
    return HttpResponseRedirect(reverse("root"))


def LogoutView(request):
    logout(request)
    return render(request, "logout.html", {})
