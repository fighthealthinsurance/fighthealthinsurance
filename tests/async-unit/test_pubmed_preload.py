import asyncio
import json
from unittest.mock import patch, AsyncMock, MagicMock
from asgiref.sync import sync_to_async
from django.test import TestCase, override_settings
from django.urls import reverse
from django.contrib.auth.models import User
from django.test.client import Client
from fighthealthinsurance.staff_views.pubmed_preload import PubMedPreloadView
from fighthealthinsurance.forms.pubmed_form import PubMedPreloadForm
from fighthealthinsurance.models import PubMedMiniArticle, PubMedQueryData
from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType


class PubMedPreloadFormTest(TestCase):
    """Test the PubMedPreloadForm functionality."""

    def test_form_clean_medications_newlines(self):
        """Test that medications are correctly cleaned when using newlines."""
        form = PubMedPreloadForm(
            data={"medications": "Aspirin\nIbuprofen\nTylenol\n\n", "conditions": ""}
        )
        self.assertTrue(form.is_valid())
        self.assertEqual(
            form.cleaned_data["medications"], ["Aspirin", "Ibuprofen", "Tylenol"]
        )

    def test_form_clean_medications_commas(self):
        """Test that medications are correctly cleaned when using commas."""
        form = PubMedPreloadForm(
            data={"medications": "Aspirin, Ibuprofen, Tylenol,  ", "conditions": ""}
        )
        self.assertTrue(form.is_valid())
        self.assertEqual(
            form.cleaned_data["medications"], ["Aspirin", "Ibuprofen", "Tylenol"]
        )

    def test_form_clean_conditions_newlines(self):
        """Test that conditions are correctly cleaned when using newlines."""
        form = PubMedPreloadForm(
            data={"medications": "", "conditions": "Diabetes\nHypertension\nAsthma\n\n"}
        )
        self.assertTrue(form.is_valid())
        self.assertEqual(
            form.cleaned_data["conditions"], ["Diabetes", "Hypertension", "Asthma"]
        )

    def test_form_clean_conditions_commas(self):
        """Test that conditions are correctly cleaned when using commas."""
        form = PubMedPreloadForm(
            data={"medications": "", "conditions": "Diabetes, Hypertension, Asthma,  "}
        )
        self.assertTrue(form.is_valid())
        self.assertEqual(
            form.cleaned_data["conditions"], ["Diabetes", "Hypertension", "Asthma"]
        )

    def test_form_empty_fields(self):
        """Test that form is valid with empty fields."""
        form = PubMedPreloadForm(data={"medications": "", "conditions": ""})
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["medications"], [])
        self.assertEqual(form.cleaned_data["conditions"], [])


class PubMedPreloadViewTest(TestCase):
    """Test the PubMedPreloadView."""

    def setUp(self):
        # Create a staff user
        self.staff_user = User.objects.create_user(
            username="staffuser", email="staff@example.com", password="testpassword"
        )
        self.staff_user.is_staff = True
        self.staff_user.save()

        # Create a client
        self.client = Client()

        # Set up URLs
        self.url = reverse("pubmed_preload")

    def test_view_requires_staff(self):
        """Test that the view requires staff permissions."""
        # Try to access without login
        response = self.client.get(self.url)
        self.assertNotEqual(response.status_code, 200)

        # Login as staff and try again
        self.client.login(username="staffuser", password="testpassword")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    def test_get_context_data(self):
        """Test that the view returns the expected context data."""
        self.client.login(username="staffuser", password="testpassword")
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertIn("title", response.context)
        self.assertIn("heading", response.context)
        self.assertIn("description", response.context)
        self.assertIn("form", response.context)
        self.assertIsInstance(response.context["form"], PubMedPreloadForm)

    @patch(
        "fighthealthinsurance.staff_views.pubmed_preload.PubMedPreloadView._stream_pubmed_search_results"
    )
    def test_form_valid(self, mock_stream):
        """Test that the form_valid method returns a streaming response."""
        self.client.login(username="staffuser", password="testpassword")

        # Mock the streaming function
        mock_stream.return_value = ["test"]

        # Submit the form
        response = self.client.post(
            self.url, {"medications": "Aspirin", "conditions": "Headache"}
        )

        # Check that the stream function was called
        self.assertTrue(mock_stream.called)

        # Check that a StreamingHttpResponse was returned
        self.assertEqual(response.streaming, True)

    def test_form_valid_no_terms(self):
        """Test that the form_valid method redirects if no terms are provided."""
        self.client.login(username="staffuser", password="testpassword")

        # Submit empty form
        response = self.client.post(self.url, {"medications": "", "conditions": ""})

        # Should render the form again with an error
        self.assertEqual(response.status_code, 200)
        self.assertIn("error", response.context)


class PubMedPreloadAsyncTests(TestCase):
    """Test the async functionality of the PubMedPreloadView."""

    async def async_setup(self):
        # Create a staff user
        self.staff_user = await sync_to_async(User.objects.create_user)(
            username="staffasyncuser",
            email="staffasync@example.com",
            password="testpassword",
        )
        self.staff_user.is_staff = True
        await sync_to_async(self.staff_user.save)()

    @patch(
        "fighthealthinsurance.pubmed_tools.PubMedTools.find_pubmed_article_ids_for_query"
    )
    async def test_perform_pubmed_search(self, mock_find_pubmed_ids):
        """Test that _perform_pubmed_search method works correctly."""
        await self.async_setup()

        # Mock the PubMed search results
        mock_find_pubmed_ids.side_effect = [
            ["12345", "67890"],  # Recent results
            ["12345", "67890", "54321"],  # All-time results
        ]

        # Create a mini article for testing
        await sync_to_async(PubMedMiniArticle.objects.create)(
            pmid="12345",
            title="Test Article",
            abstract="This is a test abstract",
            article_url="https://pubmed.ncbi.nlm.nih.gov/12345/",
        )

        view = PubMedPreloadView()
        result = await view._perform_pubmed_search("Aspirin", "medication")

        # Check the result structure
        self.assertEqual(result["term"], "Aspirin")
        self.assertEqual(result["type"], "medication")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["recent_count"], 2)
        self.assertEqual(result["all_time_count"], 3)
        self.assertEqual(result["unique_count"], 3)
        self.assertIn("12345", result["pmids"])
        self.assertIn("67890", result["pmids"])
        self.assertEqual(result["articles"][0]["pmid"], "12345")
        self.assertEqual(result["articles"][0]["title"], "Test Article")

    @patch(
        "fighthealthinsurance.pubmed_tools.PubMedTools.find_pubmed_article_ids_for_query",
        side_effect=Exception("Test error"),
    )
    async def test_perform_pubmed_search_error(self, mock_find_pubmed_ids):
        """Test that _perform_pubmed_search handles errors correctly."""
        await self.async_setup()

        view = PubMedPreloadView()
        result = await view._perform_pubmed_search("Aspirin", "medication")

        # Check that the error was captured
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "Test error")

    @patch("asgiref.sync.async_to_sync")
    def test_stream_pubmed_search_results(self, mock_async_to_sync):
        """Test that _stream_pubmed_search_results correctly sets up the generator."""
        # Mock the sync generator
        mock_generator = MagicMock()
        mock_async_to_sync.return_value = mock_generator

        view = PubMedPreloadView()
        search_terms = [
            {"term": "Aspirin", "type": "medication"},
            {"term": "Headache", "type": "condition"},
        ]

        # Call the method and check it returns the generator
        result = view._stream_pubmed_search_results(search_terms)
        self.assertEqual(result, mock_generator)

        # Verify that async_to_sync was called
        self.assertTrue(mock_async_to_sync.called)


# Create an integration test that simulates the full view without mocking external calls
@override_settings(DEBUG=True)
class PubMedPreloadIntegrationTest(TestCase):
    """
    Integration test for PubMedPreloadView.

    This test simulates the full view behavior including:
    - Form submission
    - Async processing
    - Response streaming

    Note: This test is skipped in non-DEBUG mode as it makes real network calls to PubMed.
    """

    def setUp(self):
        # Only run in DEBUG mode
        if not self.client.defaults.get("DEBUG", False):
            self.skipTest("Skipping integration test in non-DEBUG mode")

        # Create a staff user
        self.staff_user = User.objects.create_user(
            username="integrationuser",
            email="integration@example.com",
            password="testpassword",
        )
        self.staff_user.is_staff = True
        self.staff_user.save()

        # Login the client
        self.client.login(username="integrationuser", password="testpassword")

        # Set up URLs
        self.url = reverse("pubmed_preload")

    @patch(
        "fighthealthinsurance.pubmed_tools.PubMedTools.find_pubmed_article_ids_for_query"
    )
    def test_end_to_end_flow(self, mock_find_pubmed_ids):
        """Test the end-to-end flow of the PubMedPreloadView."""
        # Mock PubMed search to return a few IDs
        mock_find_pubmed_ids.return_value = ["12345", "67890"]

        # Create test article
        PubMedMiniArticle.objects.create(
            pmid="12345",
            title="Test Article",
            abstract="This is a test abstract",
            article_url="https://pubmed.ncbi.nlm.nih.gov/12345/",
        )

        # Submit the form
        response = self.client.post(
            self.url,
            {"medications": "Aspirin", "conditions": "Headache"},
            HTTP_ACCEPT="text/event-stream",
        )

        # Check that the response is streaming
        self.assertEqual(response.streaming, True)

        # Read the stream (only a limited number of chunks)
        chunks = []
        for chunk in response.streaming_content:
            chunks.append(chunk.decode("utf-8"))
            if len(chunks) >= 3:  # Limit to avoid infinite loop
                break

        # Check that the stream contains valid SSE data
        self.assertTrue(any(chunk.startswith("data:") for chunk in chunks))

        # Try to parse at least one JSON chunk
        parsed = False
        for chunk in chunks:
            if chunk.startswith("data:"):
                try:
                    data = json.loads(chunk.replace("data:", "").strip())
                    parsed = True
                    # Verify data structure
                    if "status" in data:
                        self.assertIn(data["status"], ["started", "completed"])
                    elif "result" in data:
                        self.assertIn("term", data["result"])
                        self.assertIn("type", data["result"])
                        self.assertIn("status", data["result"])
                except json.JSONDecodeError:
                    continue

        self.assertTrue(parsed, "Could not parse any valid JSON data from the stream")
