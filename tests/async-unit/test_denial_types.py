import pytest
# Test the denial_base including the regexes from the initial fixtures.
from django.test import TestCase

from fighthealthinsurance.models import DenialTypes
from fighthealthinsurance.process_denial import ProcessDenialRegex

# Class for testing the denial_base logic.
class TestDenialTypes(TestCase):
    fixtures = ["fighthealthinsurance/fixtures/initial.yaml"]

    def test_denial_base_loaded(self):
        assert DenialTypes.objects.count() > 0

    @pytest.mark.asyncio
    async def test_denial_base_matches_medical_necessity(self):
        # Get the global denial type checker and feed it "x is not medically necessary" and make sure it matches the denial type "MEDICAL_NECESSITY"
        denial_type_checker = ProcessDenialRegex()
        denial_type = await denial_type_checker.get_denialtype("x is not medically necessary", None, None)
        expected_denial_type = await DenialTypes.objects.filter(name="Medically Necessary").aget()
        assert denial_type[0]== expected_denial_type