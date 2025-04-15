from django.test import TestCase
from fhi_users.models import ProfessionalDomainRelation, ProfessionalUser, UserDomain
from django.contrib.auth import get_user_model

User = get_user_model()


class ProfessionalDomainRelationSignalTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="testuser", password="testpass")
        self.professional_user = ProfessionalUser.objects.create(
            user=self.user, active=True
        )
        self.domain = UserDomain.objects.create(
            name="testdomain",
            visible_phone_number="1234567890",
            internal_phone_number="0987654321",
            active=True,
            display_name="Test Domain",
            country="USA",
            state="CA",
            city="Test City",
            address1="123 Test St",
            zipcode="12345",
        )

    def test_active_field_when_pending(self) -> None:
        relation = ProfessionalDomainRelation.objects.create(
            professional=self.professional_user,
            domain=self.domain,
            pending_domain_relation=True,
            suspended=False,
            rejected=False,
        )
        self.assertFalse(relation.active_domain_relation)

    def test_active_field_when_suspended(self) -> None:
        relation = ProfessionalDomainRelation.objects.create(
            professional=self.professional_user,
            domain=self.domain,
            pending_domain_relation=False,
            suspended=True,
            rejected=False,
        )
        self.assertFalse(relation.active_domain_relation)

    def test_active_field_when_rejected(self) -> None:
        relation = ProfessionalDomainRelation.objects.create(
            professional=self.professional_user,
            domain=self.domain,
            pending_domain_relation=False,
            suspended=False,
            rejected=True,
        )
        self.assertFalse(relation.active_domain_relation)

    def test_active_field_when_not_pending_suspended_or_rejected(self) -> None:
        relation = ProfessionalDomainRelation.objects.create(
            professional=self.professional_user,
            domain=self.domain,
            pending_domain_relation=False,
            suspended=False,
            rejected=False,
        )
        self.assertTrue(relation.active_domain_relation)
