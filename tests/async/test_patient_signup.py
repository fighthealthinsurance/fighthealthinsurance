"""
Tests for patient signup functionality.
"""

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from fhi_users.models import PatientUser

User = get_user_model()


class PatientSignupViewTests(TestCase):
    """Tests for the patient signup view."""

    def setUp(self) -> None:
        self.client = Client()

    def test_signup_form_renders(self) -> None:
        """Test that the signup form renders correctly."""
        url = "/v0/auth/signup"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Create Free Account")
        self.assertContains(response, "Optional")
        self.assertContains(response, "Email Address")
        self.assertContains(response, "Password")

    def test_signup_success(self) -> None:
        """Test successful patient signup."""
        url = "/v0/auth/signup"
        data = {
            "email": "newpatient@example.com",
            "password": "SecurePass123!",
            "confirm_password": "SecurePass123!",
            "first_name": "New",
            "last_name": "Patient",
        }

        response = self.client.post(url, data)

        # Should redirect to dashboard after signup
        self.assertEqual(response.status_code, 302)
        self.assertIn("my/dashboard", response.url)

        # Verify user was created
        self.assertTrue(User.objects.filter(email="newpatient@example.com").exists())
        user = User.objects.get(email="newpatient@example.com")
        self.assertEqual(user.first_name, "New")
        self.assertEqual(user.last_name, "Patient")
        self.assertTrue(user.is_active)

        # Verify PatientUser was created
        self.assertTrue(PatientUser.objects.filter(user=user).exists())
        patient = PatientUser.objects.get(user=user)
        self.assertTrue(patient.active)

        # Verify user is logged in
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.pk)

    def test_signup_minimal_info(self) -> None:
        """Test signup with minimal information (email and password only)."""
        url = "/v0/auth/signup"
        data = {
            "email": "minimal@example.com",
            "password": "TestPass456!",
            "confirm_password": "TestPass456!",
        }

        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)

        # Verify user was created
        self.assertTrue(User.objects.filter(email="minimal@example.com").exists())
        user = User.objects.get(email="minimal@example.com")
        self.assertEqual(user.first_name, "")
        self.assertEqual(user.last_name, "")

    def test_signup_password_mismatch(self) -> None:
        """Test that signup fails when passwords don't match."""
        url = "/v0/auth/signup"
        data = {
            "email": "test@example.com",
            "password": "Password123!",
            "confirm_password": "DifferentPassword123!",
        }

        response = self.client.post(url, data)

        # Should stay on signup page with error
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Passwords do not match")

        # User should not be created
        self.assertFalse(User.objects.filter(email="test@example.com").exists())

    def test_signup_duplicate_email(self) -> None:
        """Test that signup fails when email already exists."""
        # Create existing user
        User.objects.create_user(
            username="existing@example.com",
            email="existing@example.com",
            password="pass",
        )

        url = "/v0/auth/signup"
        data = {
            "email": "existing@example.com",
            "password": "NewPass123!",
            "confirm_password": "NewPass123!",
        }

        response = self.client.post(url, data)

        # Should stay on signup page with error
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already exists")

    def test_signup_invalid_email(self) -> None:
        """Test that signup fails with invalid email format."""
        url = "/v0/auth/signup"
        data = {
            "email": "not-an-email",
            "password": "Pass123!",
            "confirm_password": "Pass123!",
        }

        response = self.client.post(url, data)

        # Should stay on signup page with error
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="not-an-email").exists())

    def test_signup_missing_required_fields(self) -> None:
        """Test that signup fails when required fields are missing."""
        url = "/v0/auth/signup"
        data = {
            "email": "test@example.com",
            # Missing password fields
        }

        response = self.client.post(url, data)

        # Should stay on signup page with error
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(email="test@example.com").exists())

    def test_signup_link_in_navigation(self) -> None:
        """Test that signup link appears in navigation for anonymous users."""
        response = self.client.get(reverse("root"))
        self.assertContains(response, "/v0/auth/signup")
        self.assertContains(response, "Sign Up")
        self.assertContains(response, "(Optional)")

    def test_signup_link_hidden_when_logged_in(self) -> None:
        """Test that signup link is hidden for logged-in users."""
        # Create and log in a user
        user = User.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="pass",
        )
        self.client.login(username="testuser", password="pass")

        response = self.client.get(reverse("root"))
        self.assertNotContains(response, "Sign Up")

    def test_account_prompt_banner_has_signup_link(self) -> None:
        """Test that the account prompt banner includes signup link."""
        # The banner is shown on appeal pages
        response = self.client.get(reverse("scan"))
        self.assertContains(response, "/v0/auth/signup")
        self.assertContains(response, "Create Free Account")


class PatientSignupFormTests(TestCase):
    """Tests for the PatientSignupForm."""

    def test_form_validates_email_uniqueness(self) -> None:
        """Test that form validates email uniqueness."""
        from fhi_users.auth.auth_forms import PatientSignupForm

        # Create existing user
        User.objects.create_user(
            username="existing@example.com",
            email="existing@example.com",
            password="pass",
        )

        form_data = {
            "email": "existing@example.com",
            "password": "Pass123!",
            "confirm_password": "Pass123!",
        }
        form = PatientSignupForm(data=form_data)

        self.assertFalse(form.is_valid())
        self.assertIn("email", form.errors)

    def test_form_validates_password_match(self) -> None:
        """Test that form validates password confirmation."""
        from fhi_users.auth.auth_forms import PatientSignupForm

        form_data = {
            "email": "test@example.com",
            "password": "Password1",
            "confirm_password": "Password2",
        }
        form = PatientSignupForm(data=form_data)

        self.assertFalse(form.is_valid())
        self.assertIn("Passwords do not match", str(form.errors))

    def test_form_accepts_valid_data(self) -> None:
        """Test that form accepts valid data."""
        from fhi_users.auth.auth_forms import PatientSignupForm

        form_data = {
            "email": "valid@example.com",
            "password": "ValidPass123!",
            "confirm_password": "ValidPass123!",
            "first_name": "Test",
            "last_name": "User",
        }
        form = PatientSignupForm(data=form_data)

        self.assertTrue(form.is_valid(), f"Form errors: {form.errors}")
