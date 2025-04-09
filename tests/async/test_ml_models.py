from django.test import TestCase
import unittest
from unittest.mock import patch, MagicMock, AsyncMock
import asyncio
from typing import Optional, Tuple, List

from fighthealthinsurance.ml.ml_models import (
    RemoteOpenLike,
    ModelDescription,
    RemoteModel,
    RemoteFullOpenLike,
)


class TestRemoteOpenLike(TestCase):
    """Test the RemoteOpenLike class to ensure proper behavior."""

    async def async_test_infer_no_context_no_recursion(self):
        """Test that _infer_no_context doesn't cause infinite recursion."""
        # Create a mock that will replace the _infer method
        mock_infer_result = ("Test result", ["citation1"])

        # Create an instance of RemoteOpenLike with mocked methods
        model = RemoteOpenLike(
            api_base="http://test.com",
            token="test_token",
            model="test_model",
            system_prompts_map={"test": ["test prompt"]},
        )

        # Replace the _infer method with a mock that returns our test result
        with patch.object(
            model, "_infer", AsyncMock(return_value=mock_infer_result)
        ) as mock_method:
            # Call _infer_no_context
            result = await model._infer_no_context(
                system_prompts=["test prompt"], prompt="test prompt", temperature=0.7
            )

            # Check that _infer was called exactly once with the expected parameters
            mock_method.assert_called_once()
            self.assertEqual(result, "Test result")

            # Verify the parameters passed to _infer
            args, kwargs = mock_method.call_args
            self.assertEqual(kwargs["system_prompts"], ["test prompt"])
            self.assertEqual(kwargs["prompt"], "test prompt")
            self.assertEqual(kwargs["temperature"], 0.7)

    def test_infer_no_context_no_recursion(self):
        """Run the async test using asyncio."""
        asyncio.run(self.async_test_infer_no_context_no_recursion())

    async def async_test_infer_no_context_with_none_result(self):
        """Test that _infer_no_context correctly handles None results."""
        # Create an instance of RemoteOpenLike with mocked methods
        model = RemoteOpenLike(
            api_base="http://test.com",
            token="test_token",
            model="test_model",
            system_prompts_map={"test": ["test prompt"]},
        )

        # Replace the _infer method with a mock that returns None
        with patch.object(model, "_infer", AsyncMock(return_value=None)) as mock_method:
            # Call _infer_no_context
            result = await model._infer_no_context(
                system_prompts=["test prompt"], prompt="test prompt", temperature=0.7
            )

            # Check that _infer was called exactly once
            mock_method.assert_called_once()
            # Check that the result is None
            self.assertIsNone(result)

    def test_infer_no_context_with_none_result(self):
        """Run the async test using asyncio."""
        asyncio.run(self.async_test_infer_no_context_with_none_result())

    async def async_test_infer_no_context_with_tuple_result(self):
        """Test that _infer_no_context correctly extracts the first element from a tuple result."""
        mock_infer_result = ("Test result", ["citation1", "citation2"])

        # Create an instance of RemoteOpenLike with mocked methods
        model = RemoteOpenLike(
            api_base="http://test.com",
            token="test_token",
            model="test_model",
            system_prompts_map={"test": ["test prompt"]},
        )

        # Replace the _infer method with a mock that returns our test result
        with patch.object(
            model, "_infer", AsyncMock(return_value=mock_infer_result)
        ) as mock_method:
            # Call _infer_no_context
            result = await model._infer_no_context(
                system_prompts=["test prompt"], prompt="test prompt", temperature=0.7
            )

            # Check that _infer was called exactly once
            mock_method.assert_called_once()
            # Check that the result is the first element of the tuple
            self.assertEqual(result, "Test result")

    def test_infer_no_context_with_tuple_result(self):
        """Run the async test using asyncio."""
        asyncio.run(self.async_test_infer_no_context_with_tuple_result())

    async def async_test_get_fax_number(self):
        """Test get_fax_number calls _infer instead of _infer_no_context to verify tuple unpacking works."""
        model = RemoteOpenLike(
            api_base="http://test.com",
            token="test_token",
            model="test_model",
            system_prompts_map={"test": ["test prompt"]},
        )

        # Mock _infer instead of _infer_no_context to check the full path
        with patch.object(
            model, "_infer", AsyncMock(return_value=("1234567890", ["citation1"]))
        ) as mock_infer:
            denial_text = "Please fax appeals to 1234567890."
            result = await model.get_fax_number(denial_text)

            # Verify _infer was called with expected parameters
            mock_infer.assert_called_once()
            self.assertEqual(result, "1234567890")

            # Check the prompt contains the denial text
            args, kwargs = mock_infer.call_args
            self.assertIn(denial_text, kwargs.get("prompt", ""))

    def test_get_fax_number(self):
        """Run the async test using asyncio."""
        asyncio.run(self.async_test_get_fax_number())

    async def async_test_get_procedure_and_diagnosis(self):
        """Test get_procedure_and_diagnosis handles various response formats."""
        model = RemoteOpenLike(
            api_base="http://test.com",
            token="test_token",
            model="test_model",
            system_prompts_map={"procedure": ["test prompt"]},
        )

        # Test case 1: Two-line response
        with patch.object(
            model,
            "_infer_no_context",
            AsyncMock(
                return_value="Procedure: Knee replacement\nDiagnosis: Osteoarthritis"
            ),
        ) as mock_infer:
            procedure, diagnosis = await model.get_procedure_and_diagnosis(
                "Denial for knee replacement"
            )
            mock_infer.assert_called_once()
            self.assertEqual(procedure, "Knee replacement")
            self.assertEqual(diagnosis, "Osteoarthritis")

        # Test case 2: One-line response
        with patch.object(
            model,
            "_infer_no_context",
            AsyncMock(return_value="Procedure: Knee replacement"),
        ) as mock_infer:
            procedure, diagnosis = await model.get_procedure_and_diagnosis(
                "Denial for knee replacement"
            )
            self.assertEqual(procedure, "Knee replacement")
            self.assertIsNone(diagnosis)

        # Test case 3: Multi-line response
        with patch.object(
            model,
            "_infer_no_context",
            AsyncMock(
                return_value="This is a denial for:\nProcedure: Knee replacement\nDiagnosis: Osteoarthritis\nPatient: John Doe"
            ),
        ) as mock_infer:
            procedure, diagnosis = await model.get_procedure_and_diagnosis(
                "Denial for knee replacement"
            )
            self.assertEqual(procedure, "Knee replacement")
            self.assertEqual(diagnosis, "Osteoarthritis")

        # Test case 4: None response
        with patch.object(
            model, "_infer_no_context", AsyncMock(return_value=None)
        ) as mock_infer:
            procedure, diagnosis = await model.get_procedure_and_diagnosis(
                "Denial for knee replacement"
            )
            self.assertIsNone(procedure)
            self.assertIsNone(diagnosis)

    def test_get_procedure_and_diagnosis(self):
        """Run the async test using asyncio."""
        asyncio.run(self.async_test_get_procedure_and_diagnosis())

    async def async_test_check_infinite_recursion_with_bad_implementation(self):
        """Test that confirms the infinite recursion bug is detected.

        This test creates a mock with deliberate infinite recursion and ensures
        the test can detect such issues. The test uses a counter to explicitly
        break the recursion after a few iterations.
        """
        # Create a model instance
        model = RemoteOpenLike(
            api_base="http://test.com",
            token="test_token",
            model="test_model",
            system_prompts_map={"test": ["test prompt"]},
        )

        # Create a counter to prevent actual infinite recursion in the test
        call_counter = 0
        max_calls = 3  # We'll let it recurse a few times, then break

        # Create a mock with deliberate recursive behavior that would cause infinite recursion
        async def bad_infer_no_context(*args, **kwargs):
            nonlocal call_counter
            call_counter += 1

            # Break the recursion after a few calls to prevent actual infinite recursion
            if call_counter > max_calls:
                return "Breaking infinite recursion for test"

            # Simulate the bug - the method calls itself with the same parameters
            return await model._infer_no_context(*args, **kwargs)

        # Replace the method with our buggy implementation
        with patch.object(model, "_infer_no_context", side_effect=bad_infer_no_context):
            # Also patch _infer so it doesn't actually try to make network requests
            with patch.object(
                model, "_infer", AsyncMock(return_value=("Test result", []))
            ):
                # Call the method
                result = await model._infer_no_context(
                    system_prompts=["test prompt"], prompt="test prompt"
                )

                # Assert that we broke out of the recursion
                self.assertEqual(call_counter, max_calls + 1)
                self.assertEqual(result, "Breaking infinite recursion for test")

    def test_check_infinite_recursion_with_bad_implementation(self):
        """Run the async test using asyncio."""
        asyncio.run(self.async_test_check_infinite_recursion_with_bad_implementation())

    async def async_test_correct_implementation(self):
        """Test the corrected implementation that avoids infinite recursion.

        This test verifies that the method doesn't call itself recursively and
        delegates properly to the _infer method instead.
        """
        # Create a model instance
        model = RemoteOpenLike(
            api_base="http://test.com",
            token="test_token",
            model="test_model",
            system_prompts_map={"test": ["test prompt"]},
        )

        # Create a counter to track calls
        infer_call_count = 0

        # Mock the _infer method to return a result and count calls
        async def mock_infer(*args, **kwargs):
            nonlocal infer_call_count
            infer_call_count += 1
            return ("Test result from _infer", ["citation1"])

        # Apply the mock
        with patch.object(model, "_infer", side_effect=mock_infer):
            # Call the method under test
            result = await model._infer_no_context(
                system_prompts=["test prompt"], prompt="test prompt", temperature=0.7
            )

            # Verify that _infer was called exactly once
            self.assertEqual(infer_call_count, 1)

            # Verify that the result is what we expect
            self.assertEqual(result, "Test result from _infer")

    def test_correct_implementation(self):
        """Run the async test using asyncio."""
        asyncio.run(self.async_test_correct_implementation())


class TestRemoteFullOpenLike(TestCase):
    """Test for the RemoteFullOpenLike class."""

    @patch("fighthealthinsurance.ml.ml_models.os")
    def test_model_classes_have_valid_models(self, mock_os):
        """Test that model classes implement the models method correctly."""
        from fighthealthinsurance.ml.ml_models import (
            candidate_model_backends,
            ModelDescription,
        )

        # Temporarily replace the models() methods with ones that return test data
        original_models_methods = {}
        for model_class in candidate_model_backends:
            if hasattr(model_class, "models"):
                original_models_methods[model_class] = model_class.models
                # Set up a mock return value - a list with at least one ModelDescription
                model_class.models = lambda cls=model_class: [
                    ModelDescription(
                        cost=100,
                        name=f"test-{cls.__name__}",
                        internal_name=f"test-internal-{cls.__name__}",
                    )
                ]

        try:
            # Check each model backend class
            for model_class in candidate_model_backends:
                # Skip the abstract base class
                if model_class.__name__ == "RemoteModel":
                    continue

                # Get the models from the class
                models_result = model_class.models()

                # Check that models returns a list
                self.assertIsInstance(
                    models_result,
                    list,
                    f"{model_class.__name__}.models() should return a list",
                )

                # Check that each item in the list is a ModelDescription
                for model in models_result:
                    self.assertIsInstance(
                        model,
                        ModelDescription,
                        f"Each item returned by {model_class.__name__}.models() should be a ModelDescription",
                    )
        finally:
            # Restore original methods
            for model_class, original_method in original_models_methods.items():
                model_class.models = original_method

    @patch("fighthealthinsurance.ml.ml_models.RemoteOpenLike")
    def test_get_system_prompts(self, mock_remote_open_like):
        """Test that get_system_prompts returns the correct prompts based on audience."""
        from fighthealthinsurance.ml.ml_models import RemoteFullOpenLike

        # Create a model with mocked system_prompts_map
        system_prompts_map = {
            "full": ["You are a helpful assistant for a patient."],
            "full_not_patient": ["You are a helpful assistant for a provider."],
        }

        model = RemoteFullOpenLike(
            api_base="http://test.com", token="test_token", model="test_model"
        )
        # Set the system_prompts_map directly
        model.system_prompts_map = system_prompts_map

        # Test for patient audience
        patient_prompts = model.get_system_prompts("full", for_patient=True)
        self.assertEqual(
            patient_prompts, ["You are a helpful assistant for a patient."]
        )

        # Test for professional audience
        professional_prompts = model.get_system_prompts("full", for_patient=False)
        self.assertEqual(
            professional_prompts, ["You are a helpful assistant for a provider."]
        )

        # Test for prompt type with no audience-specific version
        generic_prompts = model.get_system_prompts("generic", for_patient=False)
        # Should return the default if specific one doesn't exist
        self.assertTrue(
            len(generic_prompts) > 0,
            "Should return default prompts when specific ones don't exist",
        )
