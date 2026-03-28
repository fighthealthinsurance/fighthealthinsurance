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
from fighthealthinsurance.pubmed_tools import PubMedTools, PER_QUERY
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
            """ + str(list(range(0, 500))),
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
            "fighthealthinsurance.rest_views.pubmed_tools"
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

        # Create per-query cache data (no denial_id, as real API caching does)
        PubMedQueryData.objects.create(
            query="physical therapy rheumatoid arthritis",
            articles=json.dumps(["12345678", "87654321"]),
            created=timezone.now(),
        )

        # Create denial-level summary row (with denial_id set)
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


class PubMedE2EAppealFlowTest(TransactionTestCase):
    """E2E tests validating PubMed query flow through to appeal context generation.

    Tests the full pipeline:
      find_pubmed_articles_for_denial → per-query cache rows + denial summary row
      → find_context_for_denial → formatted context string for appeal generation.
    """

    def setUp(self):
        self.denial = Denial.objects.create(
            denial_text="Denied coverage for physical therapy for rheumatoid arthritis",
            procedure="physical therapy",
            diagnosis="rheumatoid arthritis",
        )
        # Pre-populate PubMedMiniArticle so find_pubmed_articles_for_denial
        # doesn't need to call the real PubMed article_by_pmid API.
        for pmid, title, abstract in [
            (
                "11111111",
                "PT effectiveness in RA",
                "Study showing PT helps RA patients...",
            ),
            (
                "22222222",
                "Exercise for arthritis",
                "Exercise reduces joint stiffness...",
            ),
            ("33333333", "Recent PT advances", "New protocols for PT in RA..."),
            ("44444444", "Older PT study", "Classic study on PT and RA outcomes..."),
        ]:
            PubMedMiniArticle.objects.create(
                pmid=pmid,
                title=title,
                abstract=abstract,
                article_url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            )
            PubMedArticleSummarized.objects.create(
                pmid=pmid,
                title=title,
                abstract=abstract,
                doi=f"10.1000/{pmid}",
                basic_summary=f"Summary for article {pmid}: {title}",
            )

    @mock.patch("fighthealthinsurance.utils.pubmed_fetcher.pmids_for_query")
    @mock.patch("fighthealthinsurance.ml.ml_router.ml_router.summarize")
    def test_e2e_without_microsite(self, mock_summarize, mock_pmids):
        """Full pipeline without microsite: articles_for_denial → context_for_denial."""

        # pmids_for_query returns different results per (query, since) combo
        def fake_pmids(query, since=None):
            if since == "2024":
                return ["11111111", "22222222"]
            return ["33333333", "44444444"]

        mock_pmids.side_effect = fake_pmids

        async def fake_summarize(*args, **kwargs):
            return "Summarized PubMed context for appeal"

        mock_summarize.side_effect = fake_summarize

        async def run_test():
            tools = PubMedTools()

            # --- Step 1: find_pubmed_articles_for_denial ---
            articles = await tools.find_pubmed_articles_for_denial(self.denial)

            # Should have found articles (PER_QUERY=2 per (query, since) combo,
            # 1 query × 2 since values = up to 4 articles)
            self.assertGreater(len(articles), 0)
            self.assertLessEqual(len(articles), PER_QUERY * 2)

            # Verify per-query cache rows were created (denial_id=NULL)
            cache_rows = await sync_to_async(
                lambda: list(PubMedQueryData.objects.filter(denial_id__isnull=True))
            )()
            self.assertGreater(len(cache_rows), 0, "Per-query cache rows should exist")
            for row in cache_rows:
                self.assertIsNone(row.denial_id_id)
                parsed = json.loads(row.articles)
                self.assertIsInstance(parsed, list)
                self.assertGreater(len(parsed), 0, "Cache rows should not be empty")

            # Verify denial summary row was created (denial_id=self.denial)
            summary_rows = await sync_to_async(
                lambda: list(PubMedQueryData.objects.filter(denial_id=self.denial))
            )()
            self.assertEqual(len(summary_rows), 1, "Exactly one denial summary row")
            summary = summary_rows[0]
            self.assertEqual(
                summary.query,
                "physical therapy rheumatoid arthritis",
                "Without microsite, query should be just procedure+diagnosis",
            )
            summary_pmids = json.loads(summary.articles)
            # Summary row should contain ALL pmids from queries, not just
            # the subset that had successful mini-article metadata fetches.
            self.assertEqual(
                sorted(summary_pmids),
                ["11111111", "22222222", "33333333", "44444444"],
            )

            # Verify the two row types don't cross-contaminate
            for cache_row in cache_rows:
                self.assertIsNone(cache_row.denial_id_id)
            self.assertIsNotNone(summary_rows[0].denial_id_id)

            # --- Step 2: find_context_for_denial (appeal generation path) ---
            # Set pubmed_ids_json so _find_context_for_denial uses pre-selected articles
            article_pmids = [a.pmid for a in articles]
            await Denial.objects.filter(denial_id=self.denial.denial_id).aupdate(
                pubmed_ids_json=article_pmids
            )
            await sync_to_async(self.denial.refresh_from_db)()

            context = await tools.find_context_for_denial(self.denial)

            # Context should be the summarized string
            self.assertEqual(context, "Summarized PubMed context for appeal")
            mock_summarize.assert_called()

            # --- Step 3: Verify cache is used on repeat call ---
            mock_pmids.reset_mock()
            articles2 = await tools.find_pubmed_articles_for_denial(self.denial)
            # Should use cached per-query rows and NOT call the API again
            mock_pmids.assert_not_called()
            self.assertGreater(len(articles2), 0)

        async_to_sync(run_test)()

    @mock.patch("fighthealthinsurance.pubmed_tools.get_microsite")
    @mock.patch("fighthealthinsurance.utils.pubmed_fetcher.pmids_for_query")
    @mock.patch("fighthealthinsurance.ml.ml_router.ml_router.summarize")
    def test_e2e_with_microsite(self, mock_summarize, mock_pmids, mock_get_microsite):
        """Full pipeline with microsite: microsite search terms are included in queries."""

        # Set microsite_slug on denial
        self.denial.microsite_slug = "test-arthritis"
        self.denial.save()

        # Configure mock microsite with pubmed_search_terms
        mock_microsite = mock.MagicMock()
        mock_microsite.pubmed_search_terms = [
            "arthritis biologic therapy",
            "TNF inhibitor RA",
        ]
        mock_get_microsite.return_value = mock_microsite

        call_log = []

        def fake_pmids(query, since=None):
            call_log.append((query, since))
            if "biologic" in query:
                return ["55555555", "66666666"]
            if "TNF" in query:
                return ["77777777", "88888888"]
            if since == "2024":
                return ["11111111", "22222222"]
            return ["33333333", "44444444"]

        mock_pmids.side_effect = fake_pmids

        # Add extra mini articles for microsite terms
        for pmid, title in [
            ("55555555", "Biologic therapy for RA"),
            ("66666666", "Advanced biologics"),
            ("77777777", "TNF inhibitors in RA"),
            ("88888888", "Anti-TNF outcomes"),
        ]:
            PubMedMiniArticle.objects.create(
                pmid=pmid,
                title=title,
                abstract=f"Abstract for {title}",
                article_url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            )
            PubMedArticleSummarized.objects.create(
                pmid=pmid,
                title=title,
                abstract=f"Abstract for {title}",
                doi=f"10.1000/{pmid}",
                basic_summary=f"Summary: {title}",
            )

        async def fake_summarize(*args, **kwargs):
            return "Summarized context with microsite articles"

        mock_summarize.side_effect = fake_summarize

        async def run_test():
            tools = PubMedTools()

            articles = await tools.find_pubmed_articles_for_denial(self.denial)

            # Should have articles from base query AND microsite terms
            self.assertGreater(len(articles), 0)

            # Verify microsite search terms were used in queries
            queried_terms = {q for q, s in call_log}
            self.assertIn(
                "arthritis biologic therapy",
                queried_terms,
                "Microsite search term should be queried",
            )
            self.assertIn(
                "TNF inhibitor RA",
                queried_terms,
                "Microsite search term should be queried",
            )
            self.assertIn(
                "physical therapy rheumatoid arthritis",
                queried_terms,
                "Base query should still be queried",
            )

            # Verify denial summary row has composite query string
            summary_rows = await sync_to_async(
                lambda: list(PubMedQueryData.objects.filter(denial_id=self.denial))
            )()
            self.assertEqual(len(summary_rows), 1)
            # all_queries_str is sorted and joined with " | "
            summary_query = summary_rows[0].query
            self.assertIn("|", summary_query, "Should be a composite query string")
            # All three terms should be represented
            for term in [
                "physical therapy rheumatoid arthritis",
                "arthritis biologic therapy",
                "TNF inhibitor RA",
            ]:
                self.assertIn(term, summary_query)

            # Verify per-query cache rows don't have denial_id
            cache_rows = await sync_to_async(
                lambda: list(PubMedQueryData.objects.filter(denial_id__isnull=True))
            )()
            self.assertGreater(len(cache_rows), 0)
            # Should have cache rows for each unique (query, since) combo that hit the API
            cached_queries = {r.query for r in cache_rows}
            # At minimum, the base query + microsite terms should each have cache entries
            self.assertTrue(
                len(cached_queries) >= 2,
                f"Expected cache entries for multiple queries, got: {cached_queries}",
            )

            # --- Verify context generation works ---
            article_pmids = [a.pmid for a in articles]
            await Denial.objects.filter(denial_id=self.denial.denial_id).aupdate(
                pubmed_ids_json=article_pmids
            )
            await sync_to_async(self.denial.refresh_from_db)()

            context = await tools.find_context_for_denial(self.denial)
            self.assertEqual(context, "Summarized context with microsite articles")

        async_to_sync(run_test)()

    @mock.patch("fighthealthinsurance.utils.pubmed_fetcher.pmids_for_query")
    def test_empty_results_dont_create_denial_summary(self, mock_pmids):
        """Verify that empty PubMed results don't create a denial summary row."""
        mock_pmids.return_value = []

        async def run_test():
            tools = PubMedTools()
            articles = await tools.find_pubmed_articles_for_denial(self.denial)

            self.assertEqual(len(articles), 0)

            # No denial summary row should be created
            summary_count = await sync_to_async(
                PubMedQueryData.objects.filter(denial_id=self.denial).count
            )()
            self.assertEqual(
                summary_count, 0, "Empty results should not create summary row"
            )

            # No per-query cache rows either (empty results aren't cached)
            cache_count = await sync_to_async(
                PubMedQueryData.objects.filter(denial_id__isnull=True).count
            )()
            self.assertEqual(
                cache_count, 0, "Empty results should not create cache rows"
            )

        async_to_sync(run_test)()

    @mock.patch("fighthealthinsurance.utils.pubmed_fetcher.pmids_for_query")
    def test_cache_isolation_denial_rows_not_used_as_cache(self, mock_pmids):
        """Denial summary rows must NOT be returned by per-query cache lookups."""
        # Pre-seed a denial summary row (as find_pubmed_articles_for_denial does)
        PubMedQueryData.objects.create(
            query="physical therapy rheumatoid arthritis",
            articles=json.dumps(["11111111", "22222222"]),
            denial_id=self.denial,
            created=timezone.now(),
        )

        # The API should be called because the denial row is not a valid cache hit
        mock_pmids.return_value = ["33333333", "44444444"]

        async def run_test():
            tools = PubMedTools()
            result = await tools.find_pubmed_article_ids_for_query(
                "physical therapy rheumatoid arthritis"
            )
            # Must call the API, not use the denial row
            mock_pmids.assert_called_once()
            self.assertEqual(result, ["33333333", "44444444"])

        async_to_sync(run_test)()
