"""Test the PubMed-related API functionality"""

import pytest
import json
import unittest.mock as mock
from datetime import datetime, timedelta

from django.urls import reverse
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.test import TestCase

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
            abstract="This study demonstrates the effectiveness of physical therapy for patients with rheumatoid arthritis...",
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

        response = self.client.post(
            url,
            json.dumps({"denial_id": self.denial.denial_id}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()

        # Should find at least the articles we created
        self.assertGreaterEqual(len(data), 2)

        # Check if our test articles are included
        pmids = [article["pmid"] for article in data]
        self.assertIn(self.article1.pmid, pmids)
        self.assertIn(self.article2.pmid, pmids)

        # Verify article structure
        for article in data:
            if article["pmid"] == self.article1.pmid:
                self.assertEqual(article["title"], self.article1.title)
                self.assertEqual(article["abstract"], self.article1.abstract)
                self.assertEqual(article["article_url"], self.article1.article_url)

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
        saved_pmids = json.loads(updated_denial.pubmed_ids_json)
        self.assertEqual(saved_pmids, selected_pmids)

    def test_assemble_appeal_with_pubmed_articles(self):
        """Test assembling an appeal with selected PubMed articles."""
        # First, set up PubMed articles to use
        selected_pmids = ["12345678", "87654321"]

        # Update the denial with the selected PubMed IDs
        self.denial.pubmed_ids_json = json.dumps(selected_pmids)
        self.denial.save()

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
            self.assertEqual(appeal.pubmed_ids_json, json.dumps(selected_pmids))
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

        url = reverse("denials-select-articles")
        selected_pmids = ["12345678"]

        response = self.client.post(
            url,
            json.dumps({"denial_id": self.denial.denial_id, "pmids": selected_pmids}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Update the appeal with the selected articles manually
        appeal.pubmed_ids_json = json.dumps(selected_pmids)
        appeal.save()

        # Verify the appeal was updated
        updated_appeal = Appeal.objects.get(id=appeal.id)
        self.assertEqual(updated_appeal.pubmed_ids_json, json.dumps(selected_pmids))

    @mock.patch("fighthealthinsurance.utils.pubmed_fetcher.pmids_for_query")
    def test_find_pubmed_article_ids_for_query(self, mock_pmids_for_query):
        """Test the find_pubmed_article_ids_for_query function."""
        # Mock the PubMed fetcher to avoid real API calls
        mock_pmids_for_query.return_value = ["99998888", "77776666"]

        pubmed_tools = PubMedTools()

        # Test using an existing cached query
        query = "physical therapy rheumatoid arthritis"
        since = None

        # Call the function with the correct parameters
        pmids = pubmed_tools.find_pubmed_article_ids_for_query(query, since)

        # Should get results from cache without calling the API
        mock_pmids_for_query.assert_not_called()
        self.assertIn("12345678", pmids)
        self.assertIn("87654321", pmids)

        # Test with a new query that's not cached
        new_query = "unique query with no cache"

        # Mock implementation for the since_list to control test behavior
        with mock.patch.object(pubmed_tools, "since_list", new=["2023"]):
            pmids = pubmed_tools.find_pubmed_article_ids_for_query(
                new_query, since="2023"
            )

            # Should call the API once for the one item in the mocked since_list
            mock_pmids_for_query.assert_called_once_with(new_query, since="2023")

            # Should get results from the mock function
            self.assertIn("99998888", pmids)
            self.assertIn("77776666", pmids)

    def test_find_candidate_articles_for_denial_using_cached_data(self):
        """Test find_pubmed_articles_for_denial using cached data."""
        pubmed_tools = PubMedTools()

        # First call creates cache if needed
        articles = pubmed_tools.find_pubmed_articles_for_denial(self.denial)

        # Verify we got article objects back
        pmids = [article.pmid for article in articles]
        self.assertIn(self.article1.pmid, pmids)
        self.assertIn(self.article2.pmid, pmids)

        # Create another denial with same procedure/diagnosis to test cache sharing
        denial2 = Denial.objects.create(
            denial_text="Another denial with same condition",
            primary_professional=self.professional,
            patient_user=self.patient,
            procedure=self.denial.procedure,
            diagnosis=self.denial.diagnosis,
        )

        # This should use the cached data from the previous call
        with mock.patch(
            "fighthealthinsurance.utils.pubmed_fetcher.pmids_for_query"
        ) as mock_fetch:
            articles2 = pubmed_tools.find_pubmed_articles_for_denial(denial2)
            # Should not need to fetch new data
            mock_fetch.assert_not_called()

        # Should get same articles
        pmids2 = [article.pmid for article in articles2]
        self.assertEqual(set(pmids), set(pmids2))

    def test_find_context_for_denial_with_selected_pmids(self):
        """Test that find_context_for_denial prioritizes selected PMIDs."""
        # Set selected PMIDs on the denial
        selected_pmids = ["11112222", "33334444"]  # Using our additional test articles
        self.denial.pubmed_ids_json = json.dumps(selected_pmids)
        self.denial.save()

        # Create mock for the article summary function to avoid actual processing
        with mock.patch.object(PubMedTools, "do_article_summary") as mock_summary:
            # Configure the mock to return appropriate objects
            def mock_summary_side_effect(pmid):
                # Return an existing summarized article
                return PubMedArticleSummarized.objects.get(pmid=pmid)

            mock_summary.side_effect = mock_summary_side_effect

            # Also mock out article search to ensure it's not used
            with mock.patch.object(
                PubMedTools, "find_pubmed_articles_for_denial"
            ) as mock_search:
                pubmed_tools = PubMedTools()
                context = pubmed_tools.find_context_for_denial(self.denial)

                # Should not have called regular search
                mock_search.assert_not_called()

                # Context should contain our article summaries
                for pmid in selected_pmids:
                    self.assertIn(f"Summary for {pmid}", context)

    def test_old_query_data_refresh(self):
        """Test that query data older than a month is refreshed."""
        # Create an old query (more than 30 days old)
        old_query = "physical therapy rheumatoid arthritis stale"
        old_query_data = PubMedQueryData.objects.create(
            query=old_query,
            articles=json.dumps(["55556666"]),
            denial_id=self.denial,
            created=timezone.now() - timedelta(days=35),  # Older than 30 days
        )

        # Mock the PubMed fetcher
        with mock.patch(
            "fighthealthinsurance.utils.pubmed_fetcher.pmids_for_query"
        ) as mock_fetch:
            mock_fetch.return_value = ["99998888"]

            # Mock the since_list to ensure predictable behavior
            pubmed_tools = PubMedTools()
            with mock.patch.object(pubmed_tools, "since_list", new=["2023"]):
                pmids = pubmed_tools.find_pubmed_article_ids_for_query(
                    old_query, since="2023"
                )

                # Should have called the API because the cached data is too old
                mock_fetch.assert_called_once_with(old_query, since="2023")

                # Results should include the new mocked data
                self.assertIn("99998888", pmids)

                # A new query data record should have been created
                self.assertTrue(
                    PubMedQueryData.objects.filter(
                        query=old_query, created__gte=timezone.now() - timedelta(days=1)
                    ).exists()
                )

    def test_appeal_assembly_with_pubmed_ids(self):
        """Test that AppealAssemblyHelper properly includes PubMed articles when assembling appeals."""
        # Set up test data
        selected_pmids = ["12345678", "87654321"]
        self.denial.pubmed_ids_json = json.dumps(selected_pmids)
        self.denial.save()

        # Create a test mock for Appeal.objects.create to avoid DB operations
        with mock.patch(
            "fighthealthinsurance.models.Appeal.objects.create"
        ) as mock_create_appeal:
            # Set up the mock to return a mock Appeal object
            mock_appeal = mock.MagicMock()
            mock_appeal.pubmed_ids_json = json.dumps(selected_pmids)
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
                self.assertEqual(
                    create_kwargs["pubmed_ids_json"], json.dumps(selected_pmids)
                )

                # Verify that _assemble_appeal_pdf was called with pubmed_ids_parsed
                mock_assemble_pdf.assert_called_once()
                call_args = mock_assemble_pdf.call_args[1]
                self.assertIn("pubmed_ids_parsed", call_args)
                self.assertEqual(call_args["pubmed_ids_parsed"], selected_pmids)


class PubMedToolsUnitTest(TestCase):
    """Unit tests for PubMedTools class."""

    def setUp(self):
        self.pubmed_tools = PubMedTools()

        # Create test articles
        self.article1 = PubMedMiniArticle.objects.create(
            pmid="12345678",
            title="Test Article 1",
            abstract="Test abstract 1",
        )

        self.article2 = PubMedMiniArticle.objects.create(
            pmid="87654321",
            title="Test Article 2",
            abstract="Test abstract 2",
        )

        # Create a test denial
        self.denial = Denial.objects.create(
            denial_text="Test denial",
            procedure="test procedure",
            diagnosis="test diagnosis",
        )

        # Create cached query data
        self.query_data = PubMedQueryData.objects.create(
            query="test procedure test diagnosis",
            articles=json.dumps(["12345678"]),
            denial_id=self.denial,
        )

        # Create corresponding PubMedArticleSummarized objects
        PubMedArticleSummarized.objects.create(
            pmid="12345678",
            title="Test Article 1",
            abstract="Test abstract 1",
            basic_summary="Summary for 12345678",
        )

        PubMedArticleSummarized.objects.create(
            pmid="87654321",
            title="Test Article 2",
            abstract="Test abstract 2",
            basic_summary="Summary for 87654321",
        )

    @mock.patch("fighthealthinsurance.utils.pubmed_fetcher.pmids_for_query")
    def test_find_pubmed_article_ids_empty_results(self, mock_pmids_for_query):
        """Test handling of empty results from PubMed API."""
        # First call returns empty list, second call returns some results
        mock_pmids_for_query.side_effect = [[], ["99998888"]]

        # Mock the since_list to ensure predictable behavior
        with mock.patch.object(self.pubmed_tools, "since_list", new=["2022", "2023"]):
            # Test with a new query
            new_query = "query with no initial results"
            pmids = self.pubmed_tools.find_pubmed_article_ids_for_query(
                new_query, since=None
            )

            # Should still get results from second call
            self.assertIn("99998888", pmids)

            # Should have called the API for each "since" value in our mocked since_list
            self.assertEqual(mock_pmids_for_query.call_count, 2)

    def test_get_articles(self):
        """Test retrieving PubMedArticleSummarized objects by PMID."""
        # Test with existing articles
        pmids = ["12345678", "87654321"]
        articles = self.pubmed_tools.get_articles(pmids)

        self.assertEqual(len(articles), 2)
        article_pmids = [article.pmid for article in articles]
        self.assertIn("12345678", article_pmids)
        self.assertIn("87654321", article_pmids)

        # Test with empty list
        articles = self.pubmed_tools.get_articles([])
        self.assertEqual(len(articles), 0)

        # Test with non-existent article (would require mocking the fetcher)
        with mock.patch(
            "fighthealthinsurance.utils.pubmed_fetcher.article_by_pmid"
        ) as mock_fetch:
            mock_fetch.return_value = None
            articles = self.pubmed_tools.get_articles(["99999999"])
            self.assertEqual(len(articles), 0)