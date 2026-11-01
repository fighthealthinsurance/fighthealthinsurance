from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import IntegrityError
from django.test import Client, TestCase
from django.urls import reverse

from fighthealthinsurance.evaluator_accounts import (
    EVALUATOR_GROUP_NAME,
    EVALUATOR_LOGIN_ACCOUNT_LIMIT,
)

User = get_user_model()


class EvaluatorAccountTest(TestCase):
    def setUp(self):
        self.password = "Review-pass-742!"
        self.staff = User.objects.create_user(
            username="staff-evaluator-admin",
            email="staff-evaluator-admin@example.com",
            password=self.password,
            is_staff=True,
        )
        self.add_url = reverse("add_evaluator_account")
        self.login_url = reverse("evaluator_login")

    def _evaluator(self, email="judge@example.com"):
        user = User.objects.create_user(
            username=email,
            email=email,
            password=self.password,
            is_active=True,
            is_staff=False,
            is_superuser=False,
        )
        group, _ = Group.objects.get_or_create(name=EVALUATOR_GROUP_NAME)
        user.groups.add(group)
        return user

    def _create_account(self, email="judge@example.com", **kwargs):
        data = {
            "email": email,
            "password1": self.password,
            "password2": self.password,
            **kwargs,
        }
        return self.client.post(self.add_url, data)

    def test_staff_dashboard_links_to_add_evaluators_account(self):
        self.client.force_login(self.staff)

        response = self.client.get(reverse("staff_dashboard"))

        self.assertContains(response, "Add Evaluators Account")
        self.assertContains(response, self.add_url)

    def test_anonymous_and_non_staff_users_cannot_create_accounts(self):
        regular = User.objects.create_user(
            username="regular", email="regular@example.com", password=self.password
        )
        original_count = User.objects.count()

        for user in (None, regular):
            with self.subTest(user=user):
                self.client.logout()
                if user is not None:
                    self.client.force_login(user)
                self.assertEqual(self.client.get(self.add_url).status_code, 302)
                self.assertEqual(self._create_account().status_code, 302)
                self.assertEqual(User.objects.count(), original_count)

    def test_staff_creates_an_active_permissionless_evaluator(self):
        self.client.force_login(self.staff)

        response = self._create_account(email="Judge@Example.com")

        self.assertRedirects(response, reverse("staff_dashboard"))
        user = User.objects.get(username="judge@example.com")
        self.assertEqual(user.email, "judge@example.com")
        self.assertTrue(user.is_active)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertFalse(user.user_permissions.exists())
        self.assertTrue(user.groups.filter(name=EVALUATOR_GROUP_NAME).exists())
        self.assertTrue(user.check_password(self.password))

    def test_duplicate_account_is_rejected_case_insensitively(self):
        self._evaluator()
        self.client.force_login(self.staff)
        original_count = User.objects.count()

        response = self._create_account(email="JUDGE@example.com")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already exists")
        self.assertEqual(User.objects.count(), original_count)

    def test_concurrent_account_creation_shows_a_form_error(self):
        self.client.force_login(self.staff)

        with patch(
            "fighthealthinsurance.evaluator_accounts.EvaluatorAccountForm.save",
            side_effect=IntegrityError,
        ):
            response = self._create_account()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already exists")

    def test_mismatched_passwords_create_no_account(self):
        self.client.force_login(self.staff)

        response = self._create_account(password2="different-pass-123!")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="judge@example.com").exists())

    def test_password_cannot_closely_match_the_evaluator_email(self):
        self.client.force_login(self.staff)

        response = self._create_account(
            password1="judge@example.com", password2="judge@example.com"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "too similar")
        self.assertFalse(User.objects.filter(username="judge@example.com").exists())

    def test_hidden_login_is_noindex(self):
        response = self.client.get(self.login_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="robots" content="noindex,nofollow,noarchive"')
        self.assertContains(response, "Evaluator login")

    def test_evaluator_can_log_in_to_the_chooser(self):
        user = self._evaluator()

        response = self.client.post(
            self.login_url,
            {"username": user.email.upper(), "password": self.password},
        )

        self.assertRedirects(response, reverse("chooser"))
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.id)

    def test_failed_logins_are_throttled_across_clients(self):
        user = self._evaluator()

        for attempt in range(EVALUATOR_LOGIN_ACCOUNT_LIMIT):
            with self.subTest(attempt=attempt):
                response = Client().post(
                    self.login_url,
                    {"username": user.email, "password": "wrong-password"},
                )
                expected = (
                    429 if attempt == EVALUATOR_LOGIN_ACCOUNT_LIMIT - 1 else 200
                )
                self.assertEqual(response.status_code, expected)

        response = Client().post(
            self.login_url, {"username": user.email, "password": self.password}
        )
        self.assertContains(
            response, "Too many failed login attempts", status_code=429
        )

    def test_login_rejects_staff_superusers_and_non_evaluators(self):
        accounts = [
            User.objects.create_user(username="plain", password=self.password),
            self.staff,
            User.objects.create_superuser(
                username="root", email="root@example.com", password=self.password
            ),
        ]

        for user in accounts:
            with self.subTest(user=user):
                client = Client()
                response = client.post(
                    self.login_url,
                    {"username": user.username, "password": self.password},
                )
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("_auth_user_id", client.session)

    def test_evaluator_cannot_access_staff_or_django_admin(self):
        user = self._evaluator()
        self.client.post(
            self.login_url, {"username": user.email, "password": self.password}
        )

        for url in (reverse("staff_dashboard"), "/timbit/admin/"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 302)

    def test_an_authenticated_evaluator_goes_straight_to_the_chooser(self):
        user = self._evaluator()
        self.client.force_login(user)

        response = self.client.get(self.login_url)

        self.assertRedirects(response, reverse("chooser"))
