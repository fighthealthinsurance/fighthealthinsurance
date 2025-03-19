from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase
from rest_framework import status
from datetime import timedelta
from contextlib import contextmanager
import warnings
import uuid
from fighthealthinsurance.models import Appeal, User, ProfessionalUser


@contextmanager
def suppress_deprecation_warnings():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        yield


class StatisticsAPITest(APITestCase):
    @classmethod
    def setUpTestData(cls):
        """Set up data for all test methods"""
        with suppress_deprecation_warnings():
            cls.user = User.objects.create_user(
                username="testuser", email="test@example.com", password="testpass123"
            )
            cls.professional_user = ProfessionalUser.objects.create(
                user=cls.user, active=True
            )

    def setUp(self):
        """Set up data for each test method"""
        with suppress_deprecation_warnings():
            self.current_date = timezone.now()
            self.last_month = self.current_date - timedelta(days=30)

            self.pending_appeals = []
            self.sent_appeals = []

            for i in range(5):
                appeal = Appeal.objects.create(
                    uuid=str(uuid.uuid4()),
                    appeal_text=f"Current appeal {i}",
                    pending=True,
                    sent=False,
                    creating_professional=self.professional_user,
                    mod_date=self.current_date.date(),
                    hashed_email="test@example.com",
                )
                self.pending_appeals.append(appeal)

            for i in range(5):
                appeal = Appeal.objects.create(
                    uuid=str(uuid.uuid4()),
                    appeal_text=f"Current appeal sent {i}",
                    pending=False,
                    sent=True,
                    creating_professional=self.professional_user,
                    mod_date=self.current_date.date(),
                    response_date=self.current_date.date(),
                    hashed_email="test@example.com",
                )
                self.sent_appeals.append(appeal)

    def tearDown(self):
        """Clean up after each test"""
        Appeal.objects.all().delete()

    def test_get_statistics(self):
        with suppress_deprecation_warnings():
            try:
                self.client.force_authenticate(user=self.user)

                url = reverse("appeals-stats")
                response = self.client.get(url)

                print(f"\nResponse status code: {response.status_code}")
                print(f"Response data: {response.data}")

                self.assertEqual(response.status_code, status.HTTP_200_OK)

                current_appeals = Appeal.objects.filter(
                    mod_date__range=(
                        self.current_date.replace(day=1).date(),
                        self.current_date.date(),
                    )
                )
                current_pending = current_appeals.filter(pending=True).count()
                current_sent = current_appeals.filter(sent=True).count()

                print(
                    f"Database counts - Total: {current_appeals.count()}, Pending: {current_pending}, Sent: {current_sent}"
                )

                self.assertEqual(
                    response.data["current_total_appeals"],
                    10,
                    f"Expected 10 total appeals, got {response.data['current_total_appeals']}",
                )
                self.assertEqual(
                    response.data["current_pending_appeals"],
                    5,
                    f"Expected 5 pending appeals, got {response.data['current_pending_appeals']}",
                )
                self.assertEqual(
                    response.data["current_sent_appeals"],
                    5,
                    f"Expected 5 sent appeals, got {response.data['current_sent_appeals']}",
                )

            except Exception as e:
                print(f"\nTest failed with error: {str(e)}")
                raise

    # def test_statistics_unauthorized(self):
    #     with suppress_deprecation_warnings():
    #         url = reverse('appeals-stats')
    #         response = self.client.get(url)
    #         self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)