import asyncio
import json
from unittest.mock import patch, MagicMock, AsyncMock
from django.test import TestCase
from django.contrib.auth.models import User
from django.urls import reverse
from django.test.client import Client
from asyncio import Future
from asgiref.sync import sync_to_async

from fighthealthinsurance.staff_views.pubmed_preload import PubMedPreloadView


class ServerSentEventsTest(TestCase):
    """Test the Server-Sent Events functionality of the PubMedPreloadView."""

    def setUp(self):
        # Create a staff user
        self.staff_user = User.objects.create_user(
            username="sseuser", email="sse@example.com", password="testpassword"
        )
        self.staff_user.is_staff = True
        self.staff_user.save()

        # Login
        self.client = Client()
        self.client.login(username="sseuser", password="testpassword")

        # URL
        self.url = reverse("pubmed_preload")

    @patch("asgiref.sync.async_to_sync")
    def test_sse_response_structure(self, mock_async_to_sync):
        """Test that SSE responses follow the correct structure."""

        # Create a mock generator function that yields SSE formatted responses
        def mock_generator():
            yield f"data: {json.dumps({'status': 'started', 'total': 2})}\n\n"
            yield f"data: {json.dumps({'result': {'term': 'Aspirin', 'type': 'medication', 'status': 'success'}, 'completed': 1, 'total': 2})}\n\n"
            yield f"data: {json.dumps({'result': {'term': 'Headache', 'type': 'condition', 'status': 'success'}, 'completed': 2, 'total': 2})}\n\n"
            yield f"data: {json.dumps({'status': 'completed', 'total': 2})}\n\n"

        # Setup our mock to return the generator
        mock_async_to_sync.return_value = mock_generator

        # Mock the view's method to use our mocked async_to_sync
        with patch(
            "fighthealthinsurance.staff_views.pubmed_preload.PubMedPreloadView._stream_pubmed_search_results"
        ) as mock_stream:
            mock_stream.return_value = mock_generator()

            # Submit the form
            response = self.client.post(
                self.url,
                {"medications": "Aspirin", "conditions": "Headache"},
                HTTP_ACCEPT="text/event-stream",
            )

            # Check the response is streaming
            self.assertEqual(response.streaming, True)

            # Check it has the right content type
            self.assertEqual(response["Content-Type"], "text/event-stream")

            # Read the streaming content
            chunks = list(response.streaming_content)

            # Convert bytes to strings
            chunk_strings = [chunk.decode("utf-8") for chunk in chunks]

            # Check we have 4 events as expected
            self.assertEqual(len(chunk_strings), 4)

            # Verify each chunk has the correct SSE format
            for chunk in chunk_strings:
                # Should start with "data: " and end with a double newline
                self.assertTrue(chunk.startswith("data: "))
                self.assertTrue(chunk.endswith("\n\n"))

                # Extract and parse the JSON data
                json_str = chunk.replace("data: ", "").rstrip("\n\n")
                json_data = json.loads(json_str)

                # Check the data structure
                if "status" in json_data:
                    self.assertIn(json_data["status"], ["started", "completed"])
                    self.assertIn("total", json_data)
                elif "result" in json_data:
                    self.assertIn("term", json_data["result"])
                    self.assertIn("type", json_data["result"])
                    self.assertIn("status", json_data["result"])
                    self.assertIn("completed", json_data)
                    self.assertIn("total", json_data)
                else:
                    self.fail("Unexpected JSON structure")


class AsyncCompletionTest(TestCase):
    """Test the async completion functionality of the PubMedPreloadView."""

    async def asyncSetUp(self):
        # Create a staff user
        self.staff_user = await sync_to_async(User.objects.create_user)(
            username="asyncuser", email="async@example.com", password="testpassword"
        )
        self.staff_user.is_staff = True
        await sync_to_async(self.staff_user.save)()

    @patch(
        "fighthealthinsurance.pubmed_tools.PubMedTools.find_pubmed_article_ids_for_query"
    )
    async def test_as_completed_results(self, mock_find_pubmed_ids):
        """Test that asyncio.as_completed yields results as they complete."""
        await self.asyncSetUp()

        # Create a view instance
        view = PubMedPreloadView()

        # Setup the mock to return futures that complete in a specific order
        async def slow_result(*args, **kwargs):
            await asyncio.sleep(0.1)
            return ["12345", "67890"]

        async def fast_result(*args, **kwargs):
            return ["54321"]

        # First call will be slow, second call will be fast
        mock_find_pubmed_ids.side_effect = [slow_result(), fast_result()]

        # Create search terms
        search_terms = [
            {"term": "Aspirin", "type": "medication"},  # This will be slow
            {"term": "Headache", "type": "condition"},  # This will be fast
        ]

        # We need to test the internal function that uses as_completed
        async def get_results():
            tasks = [
                view._perform_pubmed_search(term["term"], term["type"])
                for term in search_terms
            ]

            # Track the order of completion
            completion_order = []

            for task in asyncio.as_completed(tasks):
                result = await task
                completion_order.append(result["term"])

            return completion_order

        # Run the test and get the completion order
        completion_order = await get_results()

        # The fast task (Headache) should complete before the slow one (Aspirin)
        self.assertEqual(completion_order, ["Headache", "Aspirin"])


class ErrorHandlingTest(TestCase):
    """Test error handling in the PubMedPreloadView."""

    def setUp(self):
        # Create a staff user
        self.staff_user = User.objects.create_user(
            username="erroruser", email="error@example.com", password="testpassword"
        )
        self.staff_user.is_staff = True
        self.staff_user.save()

        # Login
        self.client = Client()
        self.client.login(username="erroruser", password="testpassword")

        # URL
        self.url = reverse("pubmed_preload")

    @patch(
        "fighthealthinsurance.staff_views.pubmed_preload.PubMedPreloadView._perform_pubmed_search"
    )
    @patch("asgiref.sync.async_to_sync")
    def test_error_in_search(self, mock_async_to_sync, mock_perform_search):
        """Test handling of errors in the search process."""

        # Setup an error in one of the searches
        async def mock_search(term, search_type):
            if term == "Aspirin":
                return {
                    "term": term,
                    "type": search_type,
                    "status": "success",
                    "recent_count": 2,
                    "all_time_count": 5,
                    "unique_count": 5,
                    "pmids": ["12345", "67890"],
                    "articles": [],
                    "duration": 1.5,
                    "timestamp": "2025-05-20T12:00:00",
                }
            else:
                raise Exception("Test error")

        # Mock perform_search to call our async function
        mock_perform_search.side_effect = mock_search

        # Create a generator that simulates the results
        def mock_generator():
            yield f"data: {json.dumps({'status': 'started', 'total': 2})}\n\n"
            yield f"data: {json.dumps({'result': {'term': 'Aspirin', 'type': 'medication', 'status': 'success', 'recent_count': 2, 'all_time_count': 5, 'unique_count': 5, 'pmids': ['12345', '67890'], 'articles': [], 'duration': 1.5, 'timestamp': '2025-05-20T12:00:00'}, 'completed': 1, 'total': 2})}\n\n"
            yield f"data: {json.dumps({'result': {'term': 'Headache', 'type': 'condition', 'status': 'error', 'error': 'Test error', 'duration': 0.1, 'timestamp': '2025-05-20T12:00:00'}, 'completed': 2, 'total': 2})}\n\n"
            yield f"data: {json.dumps({'status': 'completed', 'total': 2})}\n\n"

        # Setup our mock to return the generator
        mock_async_to_sync.return_value = mock_generator

        # Mock the view's method to use our mocked async_to_sync
        with patch(
            "fighthealthinsurance.staff_views.pubmed_preload.PubMedPreloadView._stream_pubmed_search_results"
        ) as mock_stream:
            mock_stream.return_value = mock_generator()

            # Submit the form
            response = self.client.post(
                self.url,
                {"medications": "Aspirin", "conditions": "Headache"},
                HTTP_ACCEPT="text/event-stream",
            )

            # Check the response is streaming
            self.assertEqual(response.streaming, True)

            # Read all chunks from the streaming response
            chunks = list(response.streaming_content)
            chunk_strings = [chunk.decode("utf-8") for chunk in chunks]

            # We should have 4 SSE events (started, 2 results, completed)
            self.assertEqual(len(chunk_strings), 4)

            # Parse the error result
            error_data = None
            for chunk in chunk_strings:
                if "error" in chunk:
                    json_str = chunk.replace("data: ", "").rstrip("\n\n")
                    data = json.loads(json_str)
                    if "result" in data and data["result"].get("status") == "error":
                        error_data = data
                        break

            # Verify the error was properly reported
            self.assertIsNotNone(error_data, "No error data found in response")
            self.assertEqual(error_data["result"]["error"], "Test error")
            self.assertEqual(error_data["result"]["term"], "Headache")
            self.assertEqual(error_data["result"]["type"], "condition")
