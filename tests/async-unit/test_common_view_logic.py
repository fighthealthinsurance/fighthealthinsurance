import asyncio
import json
import io
from asgiref.sync import async_to_sync
from unittest.mock import Mock, patch
from typing import AsyncIterator
from fighthealthinsurance.common_view_logic import (
    RemoveDataHelper,
    FindNextStepsHelper,
    AppealsBackendHelper,
    NextStepInfo,
)
from fighthealthinsurance.models import Denial, DenialTypes
import pytest
from django.test import TestCase


class TestCommonViewLogic(TestCase):
    fixtures = ["fighthealthinsurance/fixtures/initial.yaml"]

    @pytest.mark.django_db
    @patch("fighthealthinsurance.common_view_logic.Denial.objects")
    def test_remove_data_for_email(self, mock_denial_objects):
        mock_denial = Mock()
        mock_denial_objects.filter.return_value.delete.return_value = 1
        RemoveDataHelper.remove_data_for_email("test@example.com")
        mock_denial_objects.filter.assert_called()
        mock_denial_objects.filter.return_value.delete.assert_called()

    @pytest.mark.django_db
    def test_find_next_steps(self):
        # Create real DenialTypes objects
        insurance_company_type = DenialTypes.objects.get(name="Insurance Company")
        medically_necessary_type = DenialTypes.objects.get(name="Medically Necessary")

        # Create a real Denial object
        email = "test@example.com"
        denial = Denial.objects.create(
            denial_id=1,
            semi_sekret="sekret",
            hashed_email=Denial.get_hashed_email(email),
        )

        # Add denial types to the denial
        denial.denial_type.add(insurance_company_type)
        denial.denial_type.add(medically_necessary_type)

        # Call the function being tested with real objects
        next_steps = FindNextStepsHelper.find_next_steps(
            denial_id=denial.denial_id,
            email=email,
            semi_sekret=denial.semi_sekret,
            procedure="prep",
            plan_id="1",
            denial_type=None,
            denial_date=None,
            diagnosis="high risk homosexual behaviour",
            insurance_company="evilco",
            claim_id=7,
        )

        # Verify the result
        self.assertIsInstance(next_steps, NextStepInfo)

        # Clean up the test data
        denial.delete()

    @pytest.mark.django_db
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_generate_appeals(self, mock_appeal_generator):
        email = "test@example.com"
        denial = Denial.objects.create(
            denial_id=1,
            semi_sekret="sekret",
            hashed_email=Denial.get_hashed_email(email),
        )

        async def async_generator(items) -> AsyncIterator[str]:
            """Test helper: Async generator yielding items with delay."""
            for item in items:
                await asyncio.sleep(0.1)
                yield item

        async def test():
            mock_appeal_generator.generate_appeals.return_value = async_generator(
                ["test"]
            )
            responses = AppealsBackendHelper.generate_appeals(
                {
                    "denial_id": 1,
                    "email": email,
                    "semi_sekret": denial.semi_sekret,
                }
            )
            buf = io.StringIO()

            async for chunk in responses:
                buf.write(chunk)

            buf.seek(0)
            string_data = buf.getvalue()

        async_to_sync(test)()
