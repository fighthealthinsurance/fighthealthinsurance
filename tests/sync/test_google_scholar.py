"""Test the Google Scholar-related functionality"""

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
    PatientUser,
    GoogleScholarMiniArticle,
    GoogleScholarQueryData,
    GoogleScholarArticleSummarized,
)
from fighthealthinsurance.google_scholar_tools import GoogleScholarTools, google_scholar_fetcher_sync

if __name__ == "__main__":
    from django.contrib.auth.models import User
else:
    User = get_user_model()


class GoogleScholarToolsTest(TransactionTestCase):
    """Test Google Scholar tools functionality."""

    def setUp(self):
        # Create a denial with procedure and diagnosis
        self.test_denial = Denial.objects.create(
            denial_text="Test denial for arthritis requiring physical therapy",
            procedure="physical therapy",
            diagnosis="rheumatoid arthritis",
        )

        # Create test Google Scholar mini articles
        self.article1 = GoogleScholarMiniArticle.objects.create(
            article_id="test_id_1",
            title="Effectiveness of physical therapy for rheumatoid arthritis",
            snippet="This study demonstrates the effectiveness of physical therapy...",
            publication_info="Journal of Arthritis, 2024",
            cited_by_count=50,
            cited_by_link="https://scholar.google.com/scholar?cites=123",
            article_url="https://example.com/article1",
        )

        self.article2 = GoogleScholarMiniArticle.objects.create(
            article_id="test_id_2",
            title="Long-term outcomes of physical therapy in arthritis patients",
            snippet="Research shows improved mobility and pain reduction...",
            publication_info="Medical Research Journal, 2023",
            cited_by_count=75,
            article_url="https://example.com/article2",
        )

        # Create summarized articles
        for article in [self.article1, self.article2]:
            GoogleScholarArticleSummarized.objects.create(
                article_id=article.article_id,
                title=article.title,
                snippet=article.snippet,
                publication_info=article.publication_info,
                cited_by_count=article.cited_by_count,
                cited_by_link=article.cited_by_link,
                article_url=article.article_url,
                basic_summary=f"Summary for {article.article_id}",
            )

        # Create a cached query
        GoogleScholarQueryData.objects.create(
            query="physical therapy rheumatoid arthritis",
            articles=json.dumps(["test_id_1", "test_id_2"]),
            denial_id=self.test_denial,
            created=timezone.now(),
        )

    def test_generate_article_id(self):
        """Test article ID generation from result data."""
        tools = GoogleScholarTools()
        
        result = {
            'title': 'Test Article',
            'title_link': 'https://example.com/test'
        }
        
        # Should generate consistent IDs
        id1 = tools._generate_article_id(result)
        id2 = tools._generate_article_id(result)
        self.assertEqual(id1, id2)
        
        # Different results should generate different IDs
        result2 = {
            'title': 'Different Article',
            'title_link': 'https://example.com/different'
        }
        id3 = tools._generate_article_id(result2)
        self.assertNotEqual(id1, id3)

    @mock.patch("fighthealthinsurance.google_scholar_tools.google_scholar_fetcher_sync")
    @mock.patch("fighthealthinsurance.ml.ml_router.ml_router.summarize")
    def test_find_google_scholar_article_ids_for_query_cached(
        self, mock_summarize, mock_fetcher
    ):
        """Test that cached queries are used when available."""
        
        async def mock_summarize_impl(*args, **kwargs):
            return "Mock summarized text"
        
        mock_summarize.side_effect = mock_summarize_impl

        # Run the async test
        async def run_test():
            tools = GoogleScholarTools()
            # Query that's already cached
            article_ids = await tools.find_google_scholar_article_ids_for_query(
                "physical therapy rheumatoid arthritis"
            )
            
            # Should return cached results without calling the scraper
            self.assertIn("test_id_1", article_ids)
            self.assertIn("test_id_2", article_ids)
            mock_fetcher.assert_not_called()

        async_to_sync(run_test)()

    @mock.patch("fighthealthinsurance.google_scholar_tools.google_scholar_fetcher_sync")
    @mock.patch("fighthealthinsurance.ml.ml_router.ml_router.summarize")
    def test_find_google_scholar_article_ids_for_query_new(
        self, mock_summarize, mock_fetcher
    ):
        """Test fetching new query results."""
        
        # Configure mocks
        async def mock_summarize_impl(*args, **kwargs):
            return "Mock summarized text"
        
        mock_summarize.side_effect = mock_summarize_impl
        
        # Mock the scraper to return test data
        mock_fetcher.return_value = [
            {
                'title': 'New Article 1',
                'title_link': 'https://example.com/new1',
                'snippet': 'This is a new article',
                'publication_info': 'New Journal, 2024',
                'cited_by_count': 10,
                'cited_by_link': '',
                'pdf_file': '',
            }
        ]

        async def run_test():
            tools = GoogleScholarTools()
            # New query not in cache
            article_ids = await tools.find_google_scholar_article_ids_for_query(
                "unique new query"
            )
            
            # Should call the scraper
            mock_fetcher.assert_called_once()
            # Should return article IDs
            self.assertEqual(len(article_ids), 1)

        async_to_sync(run_test)()

    @mock.patch("fighthealthinsurance.google_scholar_tools.google_scholar_fetcher_sync")
    @mock.patch("fighthealthinsurance.ml.ml_router.ml_router.summarize")
    def test_find_google_scholar_articles_for_denial(
        self, mock_summarize, mock_fetcher
    ):
        """Test finding Google Scholar articles for a denial."""
        
        async def mock_summarize_impl(*args, **kwargs):
            return "Mock summarized text"
        
        mock_summarize.side_effect = mock_summarize_impl

        async def run_test():
            tools = GoogleScholarTools()
            # Find articles for the test denial (should use cached query)
            articles = await tools.find_google_scholar_articles_for_denial(
                self.test_denial
            )
            
            # Should find cached articles
            self.assertGreaterEqual(len(articles), 2)
            article_ids = [a.article_id for a in articles]
            self.assertIn("test_id_1", article_ids)
            self.assertIn("test_id_2", article_ids)

        async_to_sync(run_test)()

    @mock.patch("fighthealthinsurance.ml.ml_router.ml_router.summarize")
    def test_find_context_for_denial(self, mock_summarize):
        """Test finding context for a denial using Google Scholar articles."""
        
        async def mock_summarize_impl(*args, **kwargs):
            return "Mock summarized context with article information"
        
        mock_summarize.side_effect = mock_summarize_impl

        async def run_test():
            tools = GoogleScholarTools()
            context = await tools.find_context_for_denial(self.test_denial)
            
            # Should return summarized context
            self.assertIsNotNone(context)
            self.assertGreater(len(context), 0)
            # Summarize should have been called
            mock_summarize.assert_called()

        async_to_sync(run_test)()

    @mock.patch("fighthealthinsurance.ml.ml_router.ml_router.summarize")
    def test_get_articles(self, mock_summarize):
        """Test getting Google Scholar articles by IDs."""
        
        async def mock_summarize_impl(*args, **kwargs):
            return "Mock summarized text"
        
        mock_summarize.side_effect = mock_summarize_impl

        async def run_test():
            tools = GoogleScholarTools()
            # Get existing articles from database
            articles = await tools.get_articles(["test_id_1", "test_id_2"])
            
            # Should find both articles
            self.assertEqual(len(articles), 2)
            article_ids = [a.article_id for a in articles]
            self.assertIn("test_id_1", article_ids)
            self.assertIn("test_id_2", article_ids)

        async_to_sync(run_test)()

    @mock.patch("fighthealthinsurance.ml.ml_router.ml_router.summarize")
    def test_format_article_short(self, mock_summarize):
        """Test formatting an article for context."""
        
        async def mock_summarize_impl(*args, **kwargs):
            return "Mock summary"
        
        mock_summarize.side_effect = mock_summarize_impl

        async def run_test():
            # Create a test article
            article = await GoogleScholarArticleSummarized.objects.acreate(
                article_id="format_test",
                title="Test Formatting",
                snippet="Test snippet",
                article_url="https://example.com/test",
                cited_by_count=100,
                basic_summary="This is a test summary",
            )
            
            formatted = GoogleScholarTools.format_article_short(article)
            
            # Should include title
            self.assertIn("Test Formatting", formatted)
            # Should include URL
            self.assertIn("https://example.com/test", formatted)
            # Should include citation count
            self.assertIn("100", formatted)
            # Should include summary
            self.assertIn("This is a test summary", formatted)

        async_to_sync(run_test)()


class GoogleScholarFetcherTest(TestCase):
    """Test the synchronous Google Scholar fetcher."""

    @mock.patch("google_scholar_py.CustomGoogleScholarOrganic")
    def test_google_scholar_fetcher_sync_basic(self, mock_organic_class):
        """Test basic fetching without year filter."""
        
        # Create mock instance
        mock_instance = mock.MagicMock()
        mock_organic_class.return_value = mock_instance
        
        # Mock the scrape method
        mock_instance.scrape_google_scholar_organic_results.return_value = [
            {
                'title': 'Test Article',
                'title_link': 'https://example.com/test',
                'snippet': 'Test snippet',
                'publication_info': 'Test Journal, 2024',
                'cited_by_count': 10,
                'cited_by_link': '',
                'pdf_file': '',
            }
        ]
        
        # Call the fetcher
        results = google_scholar_fetcher_sync("test query")
        
        # Should return results
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['title'], 'Test Article')
        
        # Should have called scraper with correct params
        mock_instance.scrape_google_scholar_organic_results.assert_called_once_with(
            query="test query",
            pagination=False,
            save_to_csv=False,
            save_to_json=False
        )

    @mock.patch("google_scholar_py.CustomGoogleScholarOrganic")
    def test_google_scholar_fetcher_sync_with_year_filter(self, mock_organic_class):
        """Test fetching with year filtering."""
        
        # Create mock instance
        mock_instance = mock.MagicMock()
        mock_organic_class.return_value = mock_instance
        
        # Mock results with different years
        mock_instance.scrape_google_scholar_organic_results.return_value = [
            {
                'title': 'Recent Article',
                'publication_info': 'Journal, 2024',
                'title_link': 'https://example.com/recent',
                'snippet': '',
                'cited_by_count': 0,
                'cited_by_link': '',
                'pdf_file': '',
            },
            {
                'title': 'Old Article',
                'publication_info': 'Journal, 2020',
                'title_link': 'https://example.com/old',
                'snippet': '',
                'cited_by_count': 0,
                'cited_by_link': '',
                'pdf_file': '',
            }
        ]
        
        # Call with year filter
        results = google_scholar_fetcher_sync("test query", since="2023")
        
        # Should only include articles from 2023 or later
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['title'], 'Recent Article')
