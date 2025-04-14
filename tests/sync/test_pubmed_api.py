"""Test the PubMed-related API functionality"""

import pytest
import json
import unittest.mock as mock
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

from django.urls import reverse
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.test import TestCase, TransactionTestCase
from asgiref.sync import async_to_sync, sync_to_async

from rest_framework import status
from rest_framework.test import APITestCase

from fighthealthinsurance.models import (
    Denial,
    UserDomain,
    ExtraUserProperties,
    ProfessionalUser,
    Appeal,
    PatientUser,
    PubMedMiniArticle,
    PubMedQueryData,
    PubMedArticleSummarized,
)
from fighthealthinsurance.pubmed_tools import PubMedTools
from fighthealthinsurance.common_view_logic import AppealAssemblyHelper

if __name__ == "__main__":
    from django.contrib.auth.models import User
else:
    User = get_user_model()


class PubmedApiTest(APITestCase):
    """Test the PubMed article API endpoints."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        # Create domain
        self.domain = UserDomain.objects.create(
            name="testdomain",
            visible_phone_number="1234567890",
            internal_phone_number="0987654321",
            active=True,
            display_name="Test Domain",
            business_name="Test Business",
            country="USA",
            state="CA",
            city="Test City",
            address1="123 Test St",
            zipcode="12345",
        )

        # Create professional user
        self.pro_user = User.objects.create_user(
            username=f"prouser🐼{self.domain.id}",
            password="testpass",
            email="pro@example.com",
        )
        self.pro_username = f"prouser🐼{self.domain.id}"
        self.pro_password = "testpass"
        self.professional = ProfessionalUser.objects.create(
            user=self.pro_user, active=True, npi_number="1234567890"
        )
        self.pro_user.is_active = True
        self.pro_user.save()
        ExtraUserProperties.objects.create(user=self.pro_user, email_verified=True)

        # Create patient user
        self.patient_user = User.objects.create_user(
            username="patientuser",
            password="patientpass",
            email="patient@example.com",
            first_name="Test",
            last_name="Patient",
        )
        self.patient_user.is_active = True
        self.patient_user.save()
        self.patient = PatientUser.objects.create(user=self.patient_user)

        # Create a denial
        self.denial = Denial.objects.create(
            denial_text="Test denial text about arthritis and physical therapy",
            primary_professional=self.professional,
            creating_professional=self.professional,
            patient_user=self.patient,
            hashed_email=Denial.get_hashed_email(self.patient_user.email),
            insurance_company="Test Insurance Co",
            procedure="physical therapy",
            diagnosis="rheumatoid arthritis",
            domain=self.domain,
        )

        # Create test PubMed articles
        self.article1 = PubMedMiniArticle.objects.create(
            pmid="12345678",
            title="Effectiveness of physical therapy for rheumatoid arthritis",
            abstract="""
            This study demonstrates the effectiveness of physical therapy for patients with rheumatoid arthritis...
            We need this to be loooong like over 500 chars so it triggers the summary
            """
            + str(list(range(0, 500))),
            article_url="https://pubmed.ncbi.nlm.nih.gov/12345678/",
        )

        self.article2 = PubMedMiniArticle.objects.create(
            pmid="87654321",
            title="Long-term outcomes of physical therapy in arthritis patients",
            abstract="Research shows improved mobility and pain reduction with consistent physical therapy...",
            article_url="https://pubmed.ncbi.nlm.nih.gov/87654321/",
        )

        # Create additional test articles with different PMIDs
        self.article3 = PubMedMiniArticle.objects.create(
            pmid="11112222",
            title="Recent advances in arthritis treatment",
            abstract="New research on arthritis treatment options...",
            article_url="https://pubmed.ncbi.nlm.nih.gov/11112222/",
            created=timezone.now() - timedelta(days=5),
        )

        self.article4 = PubMedMiniArticle.objects.create(
            pmid="33334444",
            title="Physical therapy protocols for inflammatory arthritis",
            abstract="Standardized protocols for treating inflammatory arthritis with physical therapy...",
            article_url="https://pubmed.ncbi.nlm.nih.gov/33334444/",
            created=timezone.now() - timedelta(days=10),
        )

        # Create corresponding PubMedArticleSummarized objects for testing
        for article in [self.article1, self.article2, self.article3, self.article4]:
            PubMedArticleSummarized.objects.create(
                pmid=article.pmid,
                title=article.title,
                abstract=article.abstract,
                doi=f"10.1000/{article.pmid}",
                basic_summary=f"Summary for {article.pmid}",
            )

        # Create a cached PubMed query
        self.query_data = PubMedQueryData.objects.create(
            query="physical therapy rheumatoid arthritis",
            articles=json.dumps(["12345678", "87654321"]),
            denial_id=self.denial,
            created=timezone.now(),
        )

        # Create additional queries with different "since" values
        self.query_data_2024 = PubMedQueryData.objects.create(
            query="physical therapy rheumatoid arthritis",
            since="2024",
            articles=json.dumps(["11112222"]),
            denial_id=self.denial,
            created=timezone.now() - timedelta(days=2),
        )

        self.query_data_2025 = PubMedQueryData.objects.create(
            query="physical therapy rheumatoid arthritis",
            since="2025",
            articles=json.dumps(["33334444"]),
            denial_id=self.denial,
            created=timezone.now() - timedelta(days=1),
        )

        # Set up session
        self.client.login(username=self.pro_username, password=self.pro_password)
        session = self.client.session
        session["domain_id"] = str(self.domain.id)
        session.save()

    def test_get_candidate_articles(self):
        """Test retrieving candidate PubMed articles for a denial."""
        url = reverse("denials-get-candidate-articles")

        # Mock the REST view's dependency on pubmed_tools module
        with mock.patch(
            "api.views.pubmed_tools"
        ) as mock_pubmed_tools:
            # Create a coroutine that returns our test articles
            async def mock_find_pubmed_articles(*args, **kwargs):
                return [self.article1, self.article2]

            # Set up the mock to return our coroutine function
            mock_pubmed_tools.find_pubmed_articles_for_denial = (
                mock_find_pubmed_articles
            )

            # Make the REST API request
            response = self.client.post(
                url,
                json.dumps({"denial_id": self.denial.denial_id}),
                content_type="application/json",
            )

            # Verify the REST API response
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            data = response.json()
            self.assertGreaterEqual(len(data), 2)

            # Verify the response contains our articles
            pmids = [article["pmid"] for article in data]
            self.assertIn(self.article1.pmid, pmids)
            self.assertIn(self.article2.pmid, pmids)

    def test_select_articles(self):
        """Test selecting PubMed articles for a denial."""
        url = reverse("denials-select-articles")
        selected_pmids = ["12345678", "87654321"]

        response = self.client.post(
            url,
            json.dumps({"denial_id": self.denial.denial_id, "pmids": selected_pmids}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify the message in the response
        self.assertIn(
            "Selected 2 articles for this context", response.json()["message"]
        )

        # Verify the denial was updated with selected PMIDs
        updated_denial = Denial.objects.get(denial_id=self.denial.denial_id)
        saved_pmids = updated_denial.pubmed_ids_json
        self.assertEqual(saved_pmids, selected_pmids)

    def test_assemble_appeal_with_pubmed_articles(self):
        """Test assembling an appeal with selected PubMed articles."""
        # First, set up PubMed articles to use
        selected_pmids = ["12345678", "87654321"]

        # Create the URL for assemble_appeal endpoint
        url = reverse("appeals-assemble-appeal")

        # Create appeal data with PubMed articles
        appeal_data = {
            "denial_id": str(self.denial.denial_id),
            "completed_appeal_text": "This appeal includes scientific evidence from PubMed articles.",
            "insurance_company": "Test Insurance Co",
            "fax_phone": "123-456-7890",
            "pubmed_articles_to_include": selected_pmids,
        }

        # Mock the PDF assembly method to avoid actual file operations
        with mock.patch.object(
            AppealAssemblyHelper, "_assemble_appeal_pdf"
        ) as mock_assemble_pdf:
            # Configure the mock to do nothing
            mock_assemble_pdf.return_value = None

            # Call the endpoint
            response = self.client.post(
                url, json.dumps(appeal_data), content_type="application/json"
            )

            # Verify the response
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
            appeal_id = response.data.get("appeal_id")
            self.assertIsNotNone(appeal_id)

            # Verify the appeal was created with the PubMed IDs
            appeal = Appeal.objects.get(id=appeal_id)
            self.assertEqual(appeal.pubmed_ids_json, selected_pmids)
            self.assertEqual(appeal.for_denial, self.denial)

            # Verify that the appeal assembly method was called with the right parameters
            mock_assemble_pdf.assert_called_once()
            call_args = mock_assemble_pdf.call_args[1]
            self.assertIn("pubmed_ids_parsed", call_args)
            self.assertEqual(call_args["pubmed_ids_parsed"], selected_pmids)

    def test_coordinate_with_appeal(self):
        """Test that selecting articles updates a pending appeal."""
        # Create a pending appeal for the denial
        appeal = Appeal.objects.create(
            for_denial=self.denial,
            pending=True,
            patient_user=self.patient,
            primary_professional=self.professional,
            creating_professional=self.professional,
        )

        url = reverse("appeals-select-articles")
        selected_pmids = ["12345678"]

        response = self.client.post(
            url,
            json.dumps({"appeal_id": appeal.id, "pmids": selected_pmids}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify the appeal was updated
        appeal.refresh_from_db()
        self.assertEqual(appeal.pubmed_ids_json, selected_pmids)

    def test_appeal_assembly_with_pubmed_ids_basic(self):
        """Test that AppealAssemblyHelper properly includes PubMed articles when assembling appeals."""
        # Set up test data
        selected_pmids = ["12345678", "87654321"]
        self.denial.pubmed_ids_json = selected_pmids
        self.denial.save()

        # Create a test mock for Appeal.objects.create to avoid DB operations
        with mock.patch(
            "fighthealthinsurance.models.Appeal.objects.create"
        ) as mock_create_appeal:
            # Set up the mock to return a mock Appeal object
            mock_appeal = mock.MagicMock()
            mock_appeal.pubmed_ids_json = selected_pmids
            mock_create_appeal.return_value = mock_appeal

            # Mock _assemble_appeal_pdf to test the correct passing of pubmed_ids_parsed
            with mock.patch.object(
                AppealAssemblyHelper,
                "_assemble_appeal_pdf",
                return_value="/tmp/mock_appeal.pdf",
            ) as mock_assemble_pdf:
                helper = AppealAssemblyHelper()

                # Call create_or_update_appeal with pubmed_ids_parsed
                appeal = helper.create_or_update_appeal(
                    denial=self.denial,
                    name="Test Patient",
                    email="test@example.com",
                    insurance_company="Test Insurance",
                    fax_phone="123-456-7890",
                    completed_appeal_text="This is a test appeal text",
                    pubmed_ids_parsed=selected_pmids,
                    company_name="Test Company",
                    include_provided_health_history=False,
                )

                # Verify that the appeal was created with the PubMed IDs
                mock_create_appeal.assert_called_once()
                create_kwargs = mock_create_appeal.call_args[1]
                self.assertEqual(create_kwargs["pubmed_ids_json"], selected_pmids)

                # Verify that _assemble_appeal_pdf was called with pubmed_ids_parsed
                mock_assemble_pdf.assert_called_once()
                call_args = mock_assemble_pdf.call_args[1]
                self.assertIn("pubmed_ids_parsed", call_args)
                self.assertEqual(call_args["pubmed_ids_parsed"], selected_pmids)


# Class for direct PubMedTools async function testing
class PubMedToolsAsyncTest(TransactionTestCase):
    """Direct testing of PubMedTools async methods using the real implementation."""

    def setUp(self):
        # Create a denial with procedure and diagnosis
        self.test_denial = Denial.objects.create(
            denial_text="Test denial for arthritis requiring physical therapy",
            procedure="physical therapy",
            diagnosis="rheumatoid arthritis",
        )

        # Create cached query data to avoid real API calls
        PubMedQueryData.objects.create(
            query="physical therapy rheumatoid arthritis",
            articles=json.dumps(["12345678", "87654321"]),
            denial_id=self.test_denial,
            created=timezone.now(),
        )

        # Create test articles that would be returned from the API
        PubMedMiniArticle.objects.create(
            pmid="12345678",
            title="Effectiveness of physical therapy for rheumatoid arthritis",
            abstract="This study demonstrates the effectiveness of physical therapy...",
            article_url="https://pubmed.ncbi.nlm.nih.gov/12345678/",
        )

        PubMedArticleSummarized.objects.create(
            pmid="12345678",
            title="Effectiveness of physical therapy for rheumatoid arthritis",
            abstract="""This study demonstrates the effectiveness of physical therapy...""",
            doi="10.1000/12345678",
            basic_summary="Summary about physical therapy for rheumatoid arthritis",
        )

    @mock.patch("fighthealthinsurance.utils.pubmed_fetcher.pmids_for_query")
    @mock.patch("fighthealthinsurance.ml.ml_router.ml_router.summarize")
    def test_find_pubmed_article_ids_for_query_real(
        self, mock_summarize, mock_pmids_for_query
    ):
        """Test the real async method with minimal mocking."""
        # Mock external API call
        mock_pmids_for_query.return_value = ["99998888", "77776666"]

        # Mock ML router summarize to avoid ML dependency
        async def mock_summarize_impl(*args, **kwargs):
            return "Mock summarized text"

        mock_summarize.side_effect = mock_summarize_impl

        # Run the actual async method
        async def run_test():
            pubmed_tools = PubMedTools()
            # First test: query that's already cached
            cached_pmids = await pubmed_tools.find_pubmed_article_ids_for_query(
                "physical therapy rheumatoid arthritis"
            )
            # Should return cached results without calling the API
            self.assertIn("12345678", cached_pmids)
            self.assertIn("87654321", cached_pmids)
            mock_pmids_for_query.assert_not_called()

            # Second test: new query that's not cached
            mock_pmids_for_query.reset_mock()
            new_pmids = await pubmed_tools.find_pubmed_article_ids_for_query(
                "unique query with no cache"
            )
            # Should call the API and return its results
            mock_pmids_for_query.assert_called_once()
            self.assertEqual(new_pmids, ["99998888", "77776666"])

        # Run the async test in a sync context
        async_to_sync(run_test)()

    @mock.patch("fighthealthinsurance.ml.ml_router.ml_router.summarize")
    def test_find_context_for_denial_real(self, mock_summarize):
        """Test the real find_context_for_denial method with minimal mocking."""

        # Configure mocks for external dependencies
        async def mock_summarize_impl(*args, **kwargs):
            return "Mock summarized text with article information"

        mock_summarize.side_effect = mock_summarize_impl

        # Set up selected PMIDs on denial
        self.test_denial.pubmed_ids_json = ["12345678"]
        self.test_denial.save()

        # Run the actual async method
        async def run_test():
            pubmed_tools = PubMedTools()
            context = await pubmed_tools.find_context_for_denial(self.test_denial)

            # Verify summarize was called with context including our article
            mock_summarize.assert_called_once()
            # Return value from the mock
            self.assertEqual(context, "Mock summarized text with article information")

        # Run the async test in a sync context
        async_to_sync(run_test)()

    @mock.patch("fighthealthinsurance.utils.pubmed_fetcher.article_by_pmid")
    @mock.patch("fighthealthinsurance.ml.ml_router.ml_router.summarize")
    def test_get_articles_real(self, mock_summarize, mock_article_by_pmid):
        """Test the real get_articles method."""

        # Configure mocks
        async def mock_summarize_impl(*args, **kwargs):
            return "Mock summarized text"

        mock_summarize.side_effect = mock_summarize_impl

        class MockPubMedArticle:
            def __init__(self):
                self.pmid = "99999999"
                self.title = "New Test Article"
                self.abstract = "New test abstract"
                self.doi = "10.1000/99999999"
                self.content = mock.MagicMock()
                self.content.text = "Full article text"

        mock_article_by_pmid.return_value = MockPubMedArticle()

        # Run the actual async method
        async def run_test():
            pubmed_tools = PubMedTools()
            # Test getting existing articles from database
            articles = await pubmed_tools.get_articles(["12345678"])

            # Should find the article in the database
            self.assertEqual(len(articles), 1)
            self.assertEqual(articles[0].pmid, "12345678")
            mock_article_by_pmid.assert_not_called()

            # Test fetching a new article not in database
            mock_article_by_pmid.reset_mock()
            with mock.patch("metapub.FindIt") as mock_findit:
                mock_findit_instance = mock.MagicMock()
                mock_findit_instance.url = "https://example.com/article.pdf"
                mock_findit.return_value = mock_findit_instance

                with mock.patch("requests.get") as mock_get:
                    mock_response = mock.MagicMock()
                    mock_response.ok = True
                    mock_response.headers = {"Content-Type": "text/html"}
                    mock_response.text = "<html>Article text</html>"
                    mock_get.return_value = mock_response

                    # Call with a PMID not in database
                    with mock.patch.object(PubMedTools, "do_article_summary"):
                        # Skip actual article summary creation
                        pubmed_tools.do_article_summary = AsyncMock(
                            return_value=PubMedArticleSummarized(
                                pmid="99999999",
                                title="New Test Article",
                                abstract="New test abstract",
                                doi="10.1000/99999999",
                                basic_summary="Mock summary",
                            )
                        )

                        new_articles = await pubmed_tools.get_articles(["99999999"])

                        # Should have called method to fetch it
                        self.assertEqual(len(new_articles), 1)
                        self.assertEqual(new_articles[0].pmid, "99999999")
                        self.assertEqual(new_articles[0].title, "New Test Article")

        # Run the async test in a sync context
        async_to_sync(run_test)()
