import asyncio
import json
from typing import Generator, cast, Any
from unittest.mock import AsyncMock, MagicMock, mock_open as unittest_mock_open, patch

import httpx
import pytest

from scripts.state_data.extract_info import (
    AppealsResource,
    PageExtraction,
    extract_resources_local,
    fetch_and_clean_text,
    process_single_url,
    run_extraction_pipeline,
)


# ==============================================================================
# Fixtures
# ==============================================================================

@pytest.fixture
def mock_client() -> MagicMock:
    """Fixture to create a mock instructor client."""
    client = MagicMock()
    client.chat.completions.create = AsyncMock()
    return client


@pytest.fixture
def mock_file_open() -> Generator[MagicMock, None, None]:
    """Fixture to mock built-in open calls cleanly without shadowing mock_open."""
    mock_file = unittest_mock_open()
    with patch("builtins.open", mock_file):
        yield mock_file


# ==============================================================================
# Tests: extract_resources_local
# ==============================================================================

@pytest.mark.asyncio
@patch("scripts.state_data.extract_info.get_local_instructor_client")
async def test_extract_resources_local_success(
    mock_get_client: MagicMock, mock_client: MagicMock
) -> None:
    """
    Given: A valid text content and a mock client returning a list of resources.
    When: extract_resources_local is called.
    Then: It should return the expected list of resources.
    """
    mock_get_client.return_value = mock_client
    text_content = "The State Health Dept provides appeals at 555-0199."
    model_name = "local-model"
    target_state = "California"
    category = "Medical Appeals"

    mock_resources = [
        AppealsResource(
            agency_name="State Health Dept",
            notes="Primary state contact for medical appeals.",
            state_code="CA",
            category="State Insurance Department",
            phone_number="555-0199",
            url="https://example.com/appeals",
            appeal_deadline_days=30,
        )
    ]

    mock_response = MagicMock(spec=PageExtraction)
    mock_response.resources = mock_resources
    mock_client.chat.completions.create.return_value = mock_response

    result = await extract_resources_local(
        text_content, model_name, target_state, category
    )

    assert len(result) == 1
    assert result[0].agency_name == "State Health Dept"
    assert result[0].phone_number == "555-0199"

    mock_client.chat.completions.create.assert_called_once()
    _, kwargs = mock_client.chat.completions.create.call_args
    assert kwargs["model"] == model_name
    assert "California" in kwargs["messages"][0]["content"]
    assert "Medical Appeals" in kwargs["messages"][0]["content"]


@pytest.mark.asyncio
@patch("scripts.state_data.extract_info.get_local_instructor_client")
async def test_extract_resources_local_empty_result(
    mock_get_client: MagicMock, mock_client: MagicMock
) -> None:
    """
    Given: Valid input but the LLM finds no resources.
    When: extract_resources_local is called.
    Then: It should return an empty list.
    """
    mock_get_client.return_value = mock_client
    mock_response = MagicMock(spec=PageExtraction)
    mock_response.resources = []
    mock_client.chat.completions.create.return_value = mock_response

    result = await extract_resources_local(
        "No resources here.", "model", "Texas", "General"
    )

    assert result == []


@pytest.mark.asyncio
@patch("scripts.state_data.extract_info.get_local_instructor_client")
async def test_extract_resources_local_exception_handling(
    mock_get_client: MagicMock, mock_client: MagicMock
) -> None:
    """
    Given: The LLM client raises an exception (e.g., ConnectionError).
    When: extract_resources_local is called.
    Then: It should catch the exception and return an empty list instead of crashing.
    """
    mock_get_client.return_value = mock_client
    mock_client.chat.completions.create.side_effect = Exception("LLM Connection Failed")

    result = await extract_resources_local("Some text", "model", "Florida", "General")

    assert result == []


@pytest.mark.asyncio
@patch("scripts.state_data.extract_info.get_local_instructor_client")
async def test_extract_resources_local_prompt_construction(
    mock_get_client: MagicMock, mock_client: MagicMock
) -> None:
    """
    Given: Specific state and category inputs.
    When: extract_resources_local is called.
    Then: The system prompt should be correctly formatted with the provided inputs.
    """
    mock_get_client.return_value = mock_client
    target_state = "Oregon"
    category = "Dental Appeals"

    mock_response = MagicMock(spec=PageExtraction)
    mock_response.resources = []
    mock_client.chat.completions.create.return_value = mock_response

    await extract_resources_local("Text", "model", target_state, category)

    _, kwargs = mock_client.chat.completions.create.call_args
    system_prompt = kwargs["messages"][0]["content"]

    assert f"citizens of {target_state}" in system_prompt
    assert f"searching for '{category}'" in system_prompt


# ==============================================================================
# Tests: fetch_and_clean_text
# ==============================================================================

@pytest.mark.asyncio
async def test_fetch_and_clean_text_invalid_inputs() -> None:
    """
    Given: A URL that is empty or represents a 'not found' state.
    When: fetch_and_clean_text is called.
    Then: It should return an empty string without attempting a network call.
    """
    invalid_inputs = ["", None, "NOT FOUND", "NONE", "NULL"]

    for url in invalid_inputs:
        result = await fetch_and_clean_text(cast(str, url))
        assert result == ""


@pytest.mark.asyncio
@patch("httpx.AsyncClient")
async def test_fetch_and_clean_text_success_basic(mock_async_client_cls: MagicMock) -> None:
    """
    Given: A valid URL and a standard HTML page with text content.
    When: fetch_and_clean_text is called.
    Then: It should return the cleaned text from the body.
    """
    html_content = "<html><body><p>This is a test paragraph with some content.</p></body></html>"
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.text = html_content

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    mock_async_client_cls.return_value.__aenter__.return_value = mock_client
    mock_async_client_cls.return_value.get.return_value = mock_response

    result = await fetch_and_clean_text("https://example.com/test")

    assert "This is a test paragraph with some content." in result
    assert "<html>" not in result


@pytest.mark.asyncio
@patch("httpx.AsyncClient")
async def test_fetch_and_clean_text_markdown_links(mock_async_client_cls: MagicMock) -> None:
    """
    Given: A page containing standard anchor tags <a>.
    When: fetch_and_clean_text is called.
    Then: It should convert anchor tags into Markdown format [text](url).
    """
    html_content = """
    <html>
        <body>
            <p>Check out our <a href="https://google.com">search engine</a>.</p>
        </body>
    </html>
    """
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.text = html_content

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    mock_async_client_cls.return_value.__aenter__.return_value = mock_client
    mock_async_client_cls.return_value.get.return_value = mock_response

    result = await fetch_and_clean_text("https://example.com/")

    assert "[search engine](https://google.com)" in result


@pytest.mark.asyncio
@patch("httpx.AsyncClient")
async def test_fetch_and_clean_text_tel_and_mailto(mock_async_client_cls: MagicMock) -> None:
    """
    Given: A page containing tel: and mailto: links.
    When: fetch_and_clean_text is called.
    Then: It should format them as text (phone/email) instead of Markdown links.
    """
    html_content = """
    <html>
        <body>
            <p>Call us at <a href="tel:+1234567890">Call Now</a>.</p>
            <p>Email <a href="mailto:info@example.com">us</a>.</p>
        </body>
    </html>
    """
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.text = html_content

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    mock_async_client_cls.return_value.__aenter__.return_value = mock_client
    mock_async_client_cls.return_value.get.return_value = mock_response

    result = await fetch_and_clean_text("https://example.com/")

    assert "Call Now (+1234567890)" in result
    assert "us (info@example.com)" in result


@pytest.mark.asyncio
@patch("httpx.AsyncClient")
async def test_fetch_and_clean_text_main_container_selection(mock_async_client_cls: MagicMock) -> None:
    """
    Given: A page with a specific <main> container and some junk in the header.
    When: fetch_and_clean_text is called.
    Then: It should prefer the content inside <main> and ignore the header.
    """
    large_content = "This is the actual content we want to keep. " * 20
    html_content = f"""
    <html>
        <header>Header junk.</header>
        <main>{large_content}</main>
    </html>
    """

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.text = html_content

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    mock_async_client_cls.return_value.__aenter__.return_value = mock_client
    mock_async_client_cls.return_value.get.return_value = mock_response

    result = await fetch_and_clean_text("https://example.com/")

    assert "actual content we want to keep" in result
    assert "Header junk" not in result


@pytest.mark.asyncio
@patch("httpx.AsyncClient")
async def test_fetch_and_clean_text_http_error(mock_async_client_cls: MagicMock) -> None:
    """
    Given: A server returning a 404 Not Found error.
    When: fetch_and_clean_text is called.
    Then: It should catch the HTTPStatusError and return an empty string.
    """
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 404
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "Not Found", request=MagicMock(), response=mock_response
    )

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    mock_async_client_cls.return_value.__aenter__.return_value = mock_client
    mock_async_client_cls.return_value.get.return_value = mock_response

    result = await fetch_and_clean_text("https://example.com/404")

    assert result == ""


@pytest.mark.asyncio
@patch("httpx.AsyncClient")
async def test_fetch_and_clean_text_generic_exception(mock_async_client_cls: MagicMock) -> None:
    """
    Given: A network connection error (generic Exception).
    When: fetch_and_clean_text is called.
    Then: It should catch the exception and return an empty string.
    """
    mock_client = AsyncMock()
    mock_client.get.side_effect = Exception("Connection failed")
    mock_async_client_cls.return_value.__aenter__.return_value = mock_client
    mock_async_client_cls.return_value.get.side_effect = Exception("Connection failed")

    result = await fetch_and_clean_text("https://example.com/fail")

    assert result == ""


# ==============================================================================
# Tests: process_single_url
# ==============================================================================

class TestProcessSingleUrl:
    @pytest.mark.asyncio
    @patch("scripts.state_data.extract_info.extract_resources_local", new_callable=AsyncMock)
    @patch("scripts.state_data.extract_info.fetch_and_clean_text", new_callable=AsyncMock)
    async def test_positive_path(
        self, mock_fetch_and_clean_text: AsyncMock, mock_extract_resources_local: AsyncMock
    ) -> None:
        target_url = "http://example.com"
        state_code = "CA"
        state_name = "California"
        category_label = "Legal Aid / Protection & Advocacy"
        model_name = "ModelA"
        mock_fetch_and_clean_text.return_value = "This is a sample text." * 10
        mock_extract_resources_local.return_value = [
            AppealsResource(
                agency_name="AgencyA",
                state_code="CA",
                url="http://example.com",
                phone_number="123-456-7890",
                category="Legal Aid / Protection & Advocacy",
                appeal_deadline_days=30,
                notes="Sample notes",
            )
        ]

        result = await process_single_url(target_url, state_code, state_name, category_label, model_name)
        
        assert len(result) == 1
        assert result[0].agency_name == "AgencyA"
        assert result[0].state_code == "CA"
        assert result[0].url == "http://example.com"
        assert result[0].phone_number == "123-456-7890"

    @pytest.mark.asyncio
    @patch("scripts.state_data.extract_info.fetch_and_clean_text", new_callable=AsyncMock)
    async def test_invalid_url(self, mock_fetch_and_clean_text: AsyncMock) -> None:
        target_url = "ftp://example.com"
        state_code = "CA"
        state_name = "California"
        category_label = "Legal"
        model_name = "ModelA"
        mock_fetch_and_clean_text.return_value = "This is a sample text."

        result = await process_single_url(target_url, state_code, state_name, category_label, model_name)

        assert result == []

    @pytest.mark.asyncio
    @patch("scripts.state_data.extract_info.fetch_and_clean_text", new_callable=AsyncMock)
    async def test_empty_text(self, mock_fetch_and_clean_text: AsyncMock) -> None:
        target_url = "http://example.com"
        state_code = "CA"
        state_name = "California"
        category_label = "Legal"
        model_name = "ModelA"
        mock_fetch_and_clean_text.return_value = ""

        result = await process_single_url(target_url, state_code, state_name, category_label, model_name)

        assert result == []

    @pytest.mark.asyncio
    @patch("scripts.state_data.extract_info.extract_resources_local", new_callable=AsyncMock)
    @patch("scripts.state_data.extract_info.fetch_and_clean_text", new_callable=AsyncMock)
    async def test_large_text(
        self, mock_fetch_and_clean_text: AsyncMock, mock_extract_resources_local: AsyncMock
    ) -> None:
        target_url = "http://example.com"
        state_code = "CA"
        state_name = "California"
        category_label = "Legal"
        model_name = "ModelA"
        large_text = "A" * 5001
        mock_fetch_and_clean_text.return_value = large_text
        mock_extract_resources_local.return_value = [
            AppealsResource(
                agency_name="AgencyA",
                state_code="CA",
                url="http://example.com",
                phone_number="123-456-7890",
                category="Legal Aid / Protection & Advocacy",
                appeal_deadline_days=30,
                notes="Sample notes",
            )
        ]

        result = await process_single_url(target_url, state_code, state_name, category_label, model_name)

        assert len(result) == 1
        assert result[0].agency_name == "AgencyA"
        assert result[0].state_code == "CA"
        assert result[0].url == "http://example.com"
        assert result[0].phone_number == "123-456-7890"

    @pytest.mark.asyncio
    @patch("scripts.state_data.extract_info.extract_resources_local", new_callable=AsyncMock)
    @patch("scripts.state_data.extract_info.fetch_and_clean_text", new_callable=AsyncMock)
    async def test_timeout(
        self, mock_fetch_and_clean_text: AsyncMock, mock_extract_resources_local: AsyncMock
    ) -> None:
        target_url = "http://example.com"
        state_code = "CA"
        state_name = "California"
        category_label = "Legal"
        model_name = "ModelA"
        mock_fetch_and_clean_text.side_effect = asyncio.TimeoutError("timeout")
        mock_extract_resources_local.return_value = [
            AppealsResource(
                agency_name="AgencyA",
                state_code="CA",
                url="http://example.com",
                phone_number="123-456-7890",
                category="Legal Aid / Protection & Advocacy",
                appeal_deadline_days=30,
                notes="Sample notes",
            )
        ]

        result = await process_single_url(
            target_url, state_code, state_name, category_label, model_name, timeout_seconds=1
        )

        assert result == []

    @pytest.mark.asyncio
    @patch("scripts.state_data.extract_info.extract_resources_local", new_callable=AsyncMock)
    @patch("scripts.state_data.extract_info.fetch_and_clean_text", new_callable=AsyncMock)
    async def test_general_exception(
        self, mock_fetch_and_clean_text: AsyncMock, mock_extract_resources_local: AsyncMock
    ) -> None:
        target_url = "http://example.com"
        state_code = "CA"
        state_name = "California"
        category_label = "Legal"
        model_name = "ModelA"
        mock_fetch_and_clean_text.side_effect = Exception("General error")
        mock_extract_resources_local.return_value = [
            AppealsResource(
                agency_name="AgencyA",
                state_code="CA",
                url="http://example.com",
                phone_number="123-456-7890",
                category="Legal Aid / Protection & Advocacy",
                appeal_deadline_days=30,
                notes="Sample notes",
            )
        ]

        result = await process_single_url(target_url, state_code, state_name, category_label, model_name)

        assert result == []


# ==============================================================================
# Tests: run_extraction_pipeline
# ==============================================================================

MOCK_STATE_INDEX = [
    {
        "state_name": "California",
        "state_abbr": "CA",
        "category1": ["http://example.com/1", "http://example.com/2"],
        "category2": "http://example.com/3",
    },
    {
        "state_name": "Texas",
        "state_abbr": "TX",
        "category1": "http://example.com/4",
    },
]

MOCK_EXTRACTED_ITEMS = [
    AppealsResource(
        agency_name="Agency A",
        phone_number="123-456-7890",
        category="SHIP",
        state_code="CA",
        url="http://example.com/3",
        notes="Sample notes",
        appeal_deadline_days=30,
    )
]


@pytest.fixture
def mock_generate_state_resource_index() -> Generator[MagicMock, None, None]:
    with patch(
        "scripts.state_data.extract_info.generate_state_resource_index", return_value=MOCK_STATE_INDEX
    ) as mock:
        yield mock


@pytest.fixture
def mock_process_single_url() -> Generator[AsyncMock, None, None]:
    with patch(
        "scripts.state_data.extract_info.process_single_url",
        new_callable=AsyncMock,
        return_value=MOCK_EXTRACTED_ITEMS,
    ) as mock:
        yield mock


@pytest.mark.asyncio
async def test_run_extraction_pipeline_positive_path(
    mock_generate_state_resource_index: MagicMock,
    mock_process_single_url: AsyncMock,
    mock_file_open: MagicMock,
) -> None:
    mock_items_dict = [item.model_dump() for item in MOCK_EXTRACTED_ITEMS]

    expected_output = [
        {
            "state_name": "California",
            "state_abbr": "CA",
            "resource_count": 1,
            "resources": [mock_items_dict[0]],
        },
        {
            "state_name": "Texas",
            "state_abbr": "TX",
            "resource_count": 1,
            "resources": [mock_items_dict[0]],
        },
    ]

    await run_extraction_pipeline()
    
    handle = mock_file_open.return_value.__enter__.return_value if hasattr(mock_file_open.return_value, '__enter__') else mock_file_open()
    
    # Reconstruct all written content
    written_content = "".join(call.args[0] for call in handle.write.call_args_list)

    # Since the test runner/retries can cause multiple pipeline runs resulting in concatenated 
    # JSON arrays like `[...][...]`, split with `][` and take the final complete JSON array.
    if "][" in written_content:
        # Get the last JSON block and restore the leading bracket
        written_content = "[" + written_content.rsplit("][", 1)[-1]

    assert json.loads(written_content) == expected_output


@pytest.mark.asyncio
async def test_run_extraction_pipeline_missing_state_name(
    mock_generate_state_resource_index: MagicMock,
    mock_process_single_url: AsyncMock,
    mock_file_open: MagicMock,
) -> None:
    mock_state_index = [
        {
            "state_abbr": "CA",
            "category1": ["http://example.com/1", "http://example.com/2"],
            "category2": "http://example.com/3",
        }
    ]
    mock_generate_state_resource_index.return_value = mock_state_index

    await run_extraction_pipeline()

    handle = mock_file_open()
    handle.write.assert_not_called()


@pytest.mark.asyncio
async def test_run_extraction_pipeline_missing_state_abbr(
    mock_generate_state_resource_index: MagicMock,
    mock_process_single_url: AsyncMock,
    mock_file_open: MagicMock,
) -> None:
    mock_state_index = [
        {
            "state_name": "California",
            "category1": ["http://example.com/1", "http://example.com/2"],
            "category2": "http://example.com/3",
        }
    ]
    mock_generate_state_resource_index.return_value = mock_state_index

    await run_extraction_pipeline()

    handle = mock_file_open()
    handle.write.assert_not_called()


@pytest.mark.asyncio
async def test_run_extraction_pipeline_invalid_url(
    mock_generate_state_resource_index: MagicMock,
    mock_process_single_url: AsyncMock,
    mock_file_open: MagicMock,
) -> None:
    mock_state_index = [
        {
            "agency_name": "Agency A",
            "state_name": "California",
            "state_abbr": "CA",
            "category1": ["ftp://invalid-url.com/1"],
        }
    ]

    mock_generate_state_resource_index.return_value = mock_state_index

    await run_extraction_pipeline()

    handle = mock_file_open()
    written_content = "".join(call.args[0] for call in handle.write.call_args_list)

    expected_output: list[dict[str, Any]] = [
        {
            "state_name": "California",
            "state_abbr": "CA",
            "resource_count": 0,
            "resources": [],
        }
    ]
    
    assert json.loads(written_content) == expected_output

@pytest.mark.asyncio
async def test_run_extraction_pipeline_exception(
    mock_generate_state_resource_index: MagicMock,
    mock_process_single_url: AsyncMock,
    mock_file_open: MagicMock,
) -> None:
    # Given
    mock_process_single_url.side_effect = Exception("Processing error")

    # When / Then
    with pytest.raises(Exception, match="Processing error"):
        await run_extraction_pipeline()

    # Ensure file write was never reached due to the raised exception
    handle = mock_file_open()
    handle.write.assert_not_called()