from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase
from rest_framework import status
from datetime import timedelta
from contextlib import contextmanager
import warnings
import uuid
from fighthealthinsurance.models import Appeal, User, PatientUser, ProfessionalUser

@contextmanager
def suppress_deprecation_warnings():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        yield

class SearchAPITest(APITestCase):
    @classmethod
    def setUpTestData(cls):
        """Set up data for all test methods"""
        with suppress_deprecation_warnings():
            # Create test users
            cls.user = User.objects.create_user(
                username='testuser',
                email='test@example.com',
                password='testpass123'
            )
            cls.professional_user = ProfessionalUser.objects.create(
                user=cls.user,
                active=True
            )

    def setUp(self):
        """Set up test data for each method"""
        with suppress_deprecation_warnings():
            # Create test appeals with different content
            self.appeal1 = Appeal.objects.create(
                uuid=str(uuid.uuid4()),
                appeal_text="Diabetes treatment appeal case",
                pending=True,
                sent=False,
                creating_professional=self.professional_user,
                mod_date=timezone.now().date(),
                hashed_email='test@example.com'
            )
            
            self.appeal2 = Appeal.objects.create(
                uuid=str(uuid.uuid4()),
                appeal_text="Heart surgery appeal request",
                pending=False,
                sent=True,
                creating_professional=self.professional_user,
                mod_date=timezone.now().date(),
                response_text="Approved for cardiac treatment",
                response_date=timezone.now().date(),
                hashed_email='test@example.com'
            )

    def tearDown(self):
        """Clean up after each test"""
        Appeal.objects.all().delete()

    def test_search_appeals_by_text(self):
        """Test searching appeals by appeal text"""
        with suppress_deprecation_warnings():
            self.client.force_authenticate(user=self.user)
            url = reverse('search-list')
            
            # Test search for 'diabetes'
            response = self.client.get(f"{url}?q=diabetes")
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(response.data['count'], 1)
            self.assertTrue('Diabetes' in response.data['results'][0]['appeal_text'])
            
            # Test search for 'heart'
            response = self.client.get(f"{url}?q=heart")
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(response.data['count'], 1)
            self.assertTrue('Heart' in response.data['results'][0]['appeal_text'])

    def test_search_appeals_no_results(self):
        """Test search with no matching results"""
        with suppress_deprecation_warnings():
            self.client.force_authenticate(user=self.user)
            url = reverse('search-list')
            
            response = self.client.get(f"{url}?q=nonexistent")
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(response.data['count'], 0)
            self.assertEqual(len(response.data['results']), 0)

    def test_search_appeals_pagination(self):
        """Test search results pagination"""
        with suppress_deprecation_warnings():
            # Create additional appeals for pagination testing
            for i in range(15):
                Appeal.objects.create(
                    uuid=str(uuid.uuid4()),
                    appeal_text=f"Test appeal case {i}",
                    pending=True,
                    sent=False,
                    creating_professional=self.professional_user,
                    mod_date=timezone.now().date(),
                    hashed_email='test@example.com'
                )

            self.client.force_authenticate(user=self.user)
            url = reverse('search-list')
            
            # Test first page
            response = self.client.get(f"{url}?q=test&page=1&page_size=10")
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(len(response.data['results']), 10)
            self.assertTrue(response.data['next'])
            self.assertFalse(response.data['previous'])
            
            # Test second page
            response = self.client.get(f"{url}?q=test&page=2&page_size=10")
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertTrue(len(response.data['results']) > 0)
            self.assertFalse(response.data['next'])
            self.assertTrue(response.data['previous'])

    def test_search_appeals_no_query(self):
        """Test search without query parameter"""
        with suppress_deprecation_warnings():
            self.client.force_authenticate(user=self.user)
            url = reverse('search-list')
            
            response = self.client.get(url)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertEqual(response.data['error'], 'Please provide a search query parameter "q"')

    def test_search_appeals_case_insensitive(self):
        """Test case-insensitive search"""
        with suppress_deprecation_warnings():
            self.client.force_authenticate(user=self.user)
            url = reverse('search-list')
            
            # Test lowercase
            response = self.client.get(f"{url}?q=diabetes")
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(response.data['count'], 1)
            
            # Test uppercase
            response = self.client.get(f"{url}?q=DIABETES")
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(response.data['count'], 1)
            
            # Test mixed case
            response = self.client.get(f"{url}?q=DiAbEtEs")
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(response.data['count'], 1)

    def test_search_appeals_special_characters(self):
        """Test search with special characters"""
        with suppress_deprecation_warnings():
            # Create appeal with special characters
            Appeal.objects.create(
                uuid=str(uuid.uuid4()),
                appeal_text="Special case: test@example.com (urgent)",
                pending=True,
                sent=False,
                creating_professional=self.professional_user,
                mod_date=timezone.now().date(),
                hashed_email='test@example.com'
            )

            self.client.force_authenticate(user=self.user)
            url = reverse('search-list')
            
            # Test search with special characters
            response = self.client.get(f"{url}?q=@example")
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(response.data['count'], 1)