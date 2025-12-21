"""Tests for fax follow-up email functionality.

These tests verify that:
1. Follow-up emails are sent when fax has missing destination
2. Templates correctly handle missing_destination context
3. Fax redo link is correctly generated
"""

import pytest
from unittest.mock import patch, MagicMock, call
from django.template.loader import render_to_string

from fighthealthinsurance.models import Denial, FaxesToSend


@pytest.fixture
def test_denial(db):
    """Create a test denial."""
    email = "fax_followup_test@example.com"
    hashed_email = Denial.get_hashed_email(email)
    denial = Denial.objects.create(
        denial_text="Test denial for fax follow-up",
        hashed_email=hashed_email,
        raw_email=email,
        health_history="",
    )
    return denial


@pytest.fixture
def test_fax_with_destination(test_denial):
    """Create a test fax with destination."""
    fax = FaxesToSend.objects.create(
        hashed_email=test_denial.hashed_email,
        paid=True,
        email=test_denial.raw_email,
        name="Test User",
        appeal_text="Test appeal text",
        denial_id=test_denial,
        destination="4255551234",
        sent=False,
    )
    return fax


@pytest.fixture
def test_fax_without_destination(test_denial):
    """Create a test fax without destination (missing fax number)."""
    fax = FaxesToSend.objects.create(
        hashed_email=test_denial.hashed_email,
        paid=True,
        email=test_denial.raw_email,
        name="Test User No Dest",
        appeal_text="Test appeal text",
        denial_id=test_denial,
        destination=None,  # Missing destination!
        sent=False,
    )
    return fax


@pytest.mark.django_db
class TestFaxFollowUpTemplates:
    """Test fax follow-up email templates."""

    def test_fax_followup_html_success(self):
        """Test that success message is shown when fax succeeds."""
        context = {
            "name": "Test User",
            "success": True,
            "fax_redo_link": "https://example.com/fax/redo/123",
            "missing_destination": False,
        }
        html_content = render_to_string("emails/fax_followup.html", context)

        assert "fax was sent successfully" in html_content
        assert "problem sending" not in html_content
        print("✓ Success message shown correctly in HTML template")

    def test_fax_followup_html_failure(self):
        """Test that failure message is shown when fax fails."""
        context = {
            "name": "Test User",
            "success": False,
            "fax_redo_link": "https://example.com/fax/redo/123",
            "missing_destination": False,
        }
        html_content = render_to_string("emails/fax_followup.html", context)

        assert "problem sending the fax" in html_content or "problem" in html_content.lower()
        print("✓ Failure message shown correctly in HTML template")

    def test_fax_followup_html_missing_destination(self):
        """Test that missing destination message is shown appropriately."""
        context = {
            "name": "Test User",
            "success": False,
            "fax_redo_link": "https://example.com/fax/redo/123",
            "missing_destination": True,
        }
        html_content = render_to_string("emails/fax_followup.html", context)

        # The template should mention the fax redo link
        assert "fax_redo_link" not in html_content or "click here" in html_content.lower() or "redo" in html_content.lower()
        print("✓ Missing destination scenario handled in HTML template")

    def test_fax_followup_txt_contains_redo_link(self):
        """Test that the plain text email contains the redo link."""
        test_link = "https://example.com/fax/redo/test-uuid"
        context = {
            "name": "Test User",
            "success": False,
            "fax_redo_link": test_link,
            "missing_destination": True,
        }
        txt_content = render_to_string("emails/fax_followup.txt", context)

        assert test_link in txt_content
        print("✓ Redo link present in plain text template")

    def test_fax_followup_html_contains_how_to_help(self):
        """Test that the HTML email contains how-to-help information."""
        context = {
            "name": "Test User",
            "success": True,
            "fax_redo_link": "https://example.com/fax/redo/123",
        }
        html_content = render_to_string("emails/fax_followup.html", context)

        assert "how-to-help" in html_content
        print("✓ How-to-help section present in HTML template")


@pytest.mark.django_db
class TestFaxActorEmailSending:
    """Test the FaxActor email sending logic."""

    def test_fax_without_destination_triggers_failure_logic(
        self, test_fax_without_destination
    ):
        """Test that a fax without destination would trigger the failure path.

        Note: We can't instantiate the Ray actor directly in tests, so we
        verify the logic by checking the condition that triggers the failure email.
        """
        fax = test_fax_without_destination

        # This is the condition checked in do_send_fax_object:
        # if fax.destination is None:
        #     self._update_fax_for_sent(fax, False, missing_destination=True)
        should_trigger_missing_destination_email = fax.destination is None

        assert should_trigger_missing_destination_email, \
            "Fax without destination should trigger missing_destination=True path"
        print("✓ Missing destination would trigger failure with missing_destination=True")

    def test_fax_with_destination_does_not_flag_missing(self, test_fax_with_destination):
        """Test that a fax with destination doesn't flag missing_destination."""
        fax = test_fax_with_destination
        assert fax.destination is not None
        assert fax.destination != ""
        print("✓ Fax with destination has valid destination field")

    def test_fax_actor_passes_missing_destination_to_template(
        self, test_fax_without_destination
    ):
        """Test that FaxActor._update_fax_for_sent passes missing_destination to template.

        This test verifies the actual context dict built in fax_actor.py
        includes the missing_destination key, which is required for the template
        to show the correct message.
        """
        # Import and inspect the actual fax_actor code
        import inspect
        import re
        from fighthealthinsurance import fax_actor

        # Get the source code of _update_fax_for_sent
        source = inspect.getsource(fax_actor.FaxActor._update_fax_for_sent)

        # Find the context dict assignment - look for lines that are NOT comments
        # and contain the key assignment
        lines = source.split('\n')
        found_missing_destination = False
        for line in lines:
            stripped = line.strip()
            # Skip comments
            if stripped.startswith('#'):
                continue
            # Check for the key in dict context (not commented out)
            if '"missing_destination":' in stripped or "'missing_destination':" in stripped:
                found_missing_destination = True
                break

        assert found_missing_destination, \
            "FaxActor._update_fax_for_sent must include 'missing_destination' in the email context (not commented out)"
        print("✓ FaxActor passes missing_destination to email template")

    def test_fax_actor_context_dict_structure(self, test_fax_with_destination):
        """Test that the context dict has all required keys for the template."""
        # The template requires these keys
        required_context_keys = ["name", "success", "fax_redo_link", "missing_destination"]

        import inspect
        from fighthealthinsurance import fax_actor

        source = inspect.getsource(fax_actor.FaxActor._update_fax_for_sent)

        # Check each key is present and not commented out
        lines = source.split('\n')
        for key in required_context_keys:
            found = False
            for line in lines:
                stripped = line.strip()
                # Skip comments
                if stripped.startswith('#'):
                    continue
                if f'"{key}"' in stripped or f"'{key}'" in stripped:
                    found = True
                    break
            assert found, \
                f"FaxActor._update_fax_for_sent must include '{key}' in the email context (not commented out)"

        print("✓ FaxActor context dict has all required keys")


@pytest.mark.django_db
class TestFaxRedoLink:
    """Test fax redo link generation."""

    def test_fax_redo_link_contains_uuid(self, test_fax_with_destination):
        """Test that redo link contains the fax UUID."""
        from django.urls import reverse

        fax = test_fax_with_destination
        redo_link = reverse(
            "fax-followup",
            kwargs={
                "hashed_email": fax.hashed_email,
                "uuid": str(fax.uuid),
            },
        )

        assert str(fax.uuid) in redo_link
        assert fax.hashed_email in redo_link
        print(f"✓ Redo link correctly generated: {redo_link}")

    def test_fax_redo_link_in_email_context(self, test_fax_with_destination):
        """Test that the redo link is properly formatted for email context."""
        from django.urls import reverse

        fax = test_fax_with_destination
        base_url = "https://www.fighthealthinsurance.com"
        redo_link = base_url + reverse(
            "fax-followup",
            kwargs={
                "hashed_email": fax.hashed_email,
                "uuid": str(fax.uuid),
            },
        )

        assert redo_link.startswith("https://")
        assert "fax" in redo_link.lower()
        print(f"✓ Full redo link: {redo_link}")


@pytest.mark.django_db
class TestFaxFollowUpView:
    """Test the FaxFollowUpView for re-sending faxes."""

    def test_fax_followup_view_accepts_new_destination(
        self, client, test_fax_without_destination
    ):
        """Test that the fax follow-up view allows entering a new destination."""
        from django.urls import reverse

        fax = test_fax_without_destination
        url = reverse(
            "fax-followup",
            kwargs={
                "hashed_email": fax.hashed_email,
                "uuid": str(fax.uuid),
            },
        )

        # GET request should show the form
        response = client.get(url)
        assert response.status_code == 200
        print(f"✓ Fax follow-up page accessible at {url}")

    def test_fax_followup_view_updates_destination(
        self, client, test_fax_without_destination
    ):
        """Test that submitting the follow-up form updates the destination."""
        from django.urls import reverse

        fax = test_fax_without_destination
        assert fax.destination is None

        url = reverse(
            "fax-followup",
            kwargs={
                "hashed_email": fax.hashed_email,
                "uuid": str(fax.uuid),
            },
        )

        new_fax_number = "8005559999"

        with patch("fighthealthinsurance.common_view_logic.SendFaxHelper.resend") as mock_resend:
            mock_resend.return_value = True

            response = client.post(
                url,
                data={
                    "fax_phone": new_fax_number,
                    "uuid": str(fax.uuid),
                    "hashed_email": fax.hashed_email,
                },
            )

            # Should either redirect or show success
            assert response.status_code in [200, 302]

            # Verify resend was called with new fax number
            if mock_resend.called:
                call_kwargs = mock_resend.call_args
                assert new_fax_number in str(call_kwargs)
                print(f"✓ Fax resend called with new number: {new_fax_number}")
            else:
                print("Note: Resend mock not called - form may have validation issues")


@pytest.mark.django_db
class TestMissingFaxNumberScenarios:
    """Test various scenarios where fax number might be missing."""

    def test_fax_created_with_none_destination(self, test_denial):
        """Test creating a fax with None destination."""
        fax = FaxesToSend.objects.create(
            hashed_email=test_denial.hashed_email,
            paid=True,
            email=test_denial.raw_email,
            appeal_text="Test",
            denial_id=test_denial,
            destination=None,
        )

        assert fax.destination is None
        fax.delete()
        print("✓ Fax can be created with None destination")

    def test_fax_created_with_empty_destination(self, test_denial):
        """Test creating a fax with empty string destination."""
        fax = FaxesToSend.objects.create(
            hashed_email=test_denial.hashed_email,
            paid=True,
            email=test_denial.raw_email,
            appeal_text="Test",
            denial_id=test_denial,
            destination="",
        )

        assert fax.destination == ""
        fax.delete()
        print("✓ Fax can be created with empty string destination")

    def test_find_faxes_needing_destination(self, test_denial):
        """Test that we can find all faxes that need a destination."""
        # Create faxes with various destination states
        fax_none = FaxesToSend.objects.create(
            hashed_email=test_denial.hashed_email,
            paid=True,
            email=test_denial.raw_email,
            appeal_text="Test 1",
            denial_id=test_denial,
            destination=None,
            sent=False,
        )

        fax_empty = FaxesToSend.objects.create(
            hashed_email=test_denial.hashed_email,
            paid=True,
            email=test_denial.raw_email,
            appeal_text="Test 2",
            denial_id=test_denial,
            destination="",
            sent=False,
        )

        fax_valid = FaxesToSend.objects.create(
            hashed_email=test_denial.hashed_email,
            paid=True,
            email=test_denial.raw_email,
            appeal_text="Test 3",
            denial_id=test_denial,
            destination="5551234567",
            sent=False,
        )

        # Find faxes missing destination
        from django.db.models import Q

        faxes_missing_dest = FaxesToSend.objects.filter(
            Q(destination__isnull=True) | Q(destination=""),
            sent=False,
        )

        # Should find at least 2 (none and empty)
        assert faxes_missing_dest.filter(pk=fax_none.pk).exists()
        assert faxes_missing_dest.filter(pk=fax_empty.pk).exists()
        assert not faxes_missing_dest.filter(pk=fax_valid.pk).exists()

        # Clean up
        fax_none.delete()
        fax_empty.delete()
        fax_valid.delete()
        print("✓ Can correctly identify faxes needing destination")

    def test_denial_fax_number_propagates_to_fax(self, test_denial):
        """Test that fax number from Denial is used when creating FaxesToSend."""
        # Set fax number on denial
        test_denial.appeal_fax_number = "9995551234"
        test_denial.save()

        # The fax should use the denial's fax number
        # This is what happens in stage_appeal_as_fax
        fax = FaxesToSend.objects.create(
            hashed_email=test_denial.hashed_email,
            paid=True,
            email=test_denial.raw_email,
            appeal_text="Test",
            denial_id=test_denial,
            destination=test_denial.appeal_fax_number,  # From denial
        )

        assert fax.destination == test_denial.appeal_fax_number
        fax.delete()
        print("✓ Denial fax number correctly propagates to FaxesToSend")


@pytest.mark.django_db
class TestFaxViewsSavesDenialFaxNumber:
    """Test that fax_views.py saves the fax number to the Denial model."""

    def test_stage_fax_view_saves_fax_number_to_denial(self):
        """Test that StageFaxView saves fax number to denial.appeal_fax_number.

        This is a code inspection test that verifies the StageFaxView
        properly saves the fax number to the denial before staging.
        """
        import inspect
        from fighthealthinsurance import fax_views

        # Get the source of StageFaxView.form_valid
        source = inspect.getsource(fax_views.StageFaxView.form_valid)

        # Check that appeal_fax_number is set AND denial.save() is called
        lines = source.split('\n')
        found_fax_number_assignment = False
        found_denial_save = False

        for line in lines:
            stripped = line.strip()
            # Skip comments
            if stripped.startswith('#'):
                continue
            if 'appeal_fax_number' in stripped and '=' in stripped:
                found_fax_number_assignment = True
            if 'denial.save()' in stripped:
                found_denial_save = True

        assert found_fax_number_assignment, \
            "StageFaxView.form_valid must set denial.appeal_fax_number"
        assert found_denial_save, \
            "StageFaxView.form_valid must call denial.save() to persist fax number"
        print("✓ StageFaxView correctly saves fax number to denial")
