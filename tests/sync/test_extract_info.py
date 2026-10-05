import pytest
import json
from unittest.mock import MagicMock, mock_open, patch
from typing import Any, List, Dict, Tuple, cast, Generator
import requests

from scripts.state_data.extract_info import (
    search_duckduckgo,
    generate_state_resource_index,
    get_local_instructor_client,
    sanitize_url,
    extract_lsc_grantee_urls
)

@pytest.fixture
def mock_ddgs() -> Generator[MagicMock, None, None]:
    """Fixture to handle repetitive DDGS mocking and patching."""
    mock_ddgs_instance = MagicMock()
    mock_ddgs_instance.__enter__.return_value = mock_ddgs_instance
    with patch("scripts.state_data.extract_info.DDGS", return_value=mock_ddgs_instance):
        yield mock_ddgs_instance

class TestSearchDuckDuckGo:
    """Tests for the search_duckduckgo function using Given-When-Then pattern."""

    def test_search_success(self, mock_ddgs: MagicMock):
        """
        Given a valid query and a successful DuckDuckGo response,
        When search_duckduckgo is called,
        Then it should return the 'href' of the first result.
        """
        mock_ddgs.text.return_value = [{"href": "https://example.com", "title": "Result"}]

        result = search_duckduckgo("test query")

        assert result == "https://example.com"
        mock_ddgs.text.assert_called_once_with("test query", max_results=1)

    def test_search_no_results(self, mock_ddgs: MagicMock):
        """
        Given a query that returns no results,
        When search_duckduckgo is called,
        Then it should return None.
        """
        mock_ddgs.text.return_value = []

        result = search_duckduckgo("nonexistent query")

        assert result is None

    @patch("time.sleep")
    def test_search_rate_limit_retry(self, mock_sleep: MagicMock, mock_ddgs: MagicMock):
        """
        Given a rate limit error (429) on the first attempt,
        When search_duckduckgo is called,
        Then it should sleep and retry, eventually returning a result on the second attempt.
        """
        mock_ddgs.text.side_effect = [
            Exception("Rate limit exceeded (429)"),
            [{"href": "https://retry.com"}]
        ]

        result = search_duckduckgo("retry query")

        assert result == "https://retry.com"
        assert mock_ddgs.text.call_count == 2
        mock_sleep.assert_called_once_with(3)

    def test_search_generic_exception(self, mock_ddgs: MagicMock):
        """
        Given a generic exception (not 429/Ratelimit),
        When search_duckduckgo is called,
        Then it should break the loop immediately and return None.
        """
        mock_ddgs.text.side_effect = Exception("Generic Error")

        result = search_duckduckgo("error query")

        assert result is None
        assert mock_ddgs.text.call_count == 1

    @patch("time.sleep")
    def test_search_max_retries_exceeded(self, mock_sleep: MagicMock, mock_ddgs: MagicMock):
        """
        Given a persistent rate limit error exceeding max_retries,
        When search_duckduckgo is called,
        Then it should return None after all attempts are exhausted.
        """
        mock_ddgs.text.side_effect = Exception("Rate limit exceeded (429)")

        result = search_duckduckgo("persistent error", max_retries=2)

        assert result is None
        assert mock_ddgs.text.call_count == 2
        assert mock_sleep.call_count == 2


@pytest.fixture
def mock_constants() -> Generator[Tuple[List[Dict[str, str]], Dict[str, str], str], None, None]:
    """
    Mock the constants used in extract_info.py to ensure a controlled 
    and fast testing environment.
    """
    mock_states = [
        {"name": "TestState", "abbr": "TS"},
        {"name": "AnotherState", "abbr": "AS"}
    ]
    mock_categories = {
        "category_1": "Search for {state_name} {state_abbr} category 1",
        "category_2": "Search for {state_name} {state_abbr} category 2"
    }
    mock_index_file = "mock_index.json"

    with patch("scripts.state_data.extract_info.STATES", mock_states), \
         patch("scripts.state_data.extract_info.SEARCH_CATEGORIES", mock_categories), \
         patch("scripts.state_data.extract_info.INDEX_FILE", mock_index_file), \
         patch("scripts.state_data.extract_info.search_duckduckgo", return_value="http://mock-url.com"), \
         patch("scripts.state_data.extract_info.extract_lsc_grantee_urls", return_value={
             "TestState": [{"profile_url": "http://test1.com"}],
             "AnotherState": [{"profile_url": "http://test2.com"}]
         }), \
         patch("time.sleep"):
        yield mock_states, mock_categories, mock_index_file


@patch("os.path.exists", return_value=False)
@patch("builtins.open", new_callable=mock_open)
def test_generate_index_fresh_build(m_open: MagicMock, mock_exists: MagicMock, mock_constants: Tuple[List[Dict[str, str]], Dict[str, str], str]):
    """
    Given: The index file does not exist.
    When: generate_state_resource_index is called with force_rebuild=False.
    Then: It should perform full searches, scrape LSC profiles, and save the file.
    """
    mock_index_file = mock_constants[2]
    
    result = generate_state_resource_index(force_rebuild=False)

    assert len(result) == 2
    assert result[0]["state_abbr"] == "TS"
    assert result[0]["category_1"] == "http://mock-url.com"
    assert result[0]["lsc_grantee_profiles"] == ["http://test1.com"]
    
    m_open.assert_called_with(mock_index_file, "w", encoding="utf-8")
    assert m_open().write.called


@patch("os.path.exists", return_value=True)
@patch("scripts.state_data.extract_info.search_duckduckgo", return_value="http://new_found.com")
@patch("scripts.state_data.extract_info.extract_lsc_grantee_urls", return_value=["http://mocked.com"])
@patch("time.sleep")
def test_generate_index_partial_update(mock_sleep: MagicMock, mock_extract: MagicMock, mock_search: MagicMock, mock_exists: MagicMock, mock_constants: Any):
    """
    Given: The index file exists but 'TestState' is missing 'category_1'.
    When: generate_state_resource_index is called with force_rebuild=False.
    Then: It should only call search_duckduckgo for the missing field.
    """
    existing_data = [
        {
            "state_name": "TestState", 
            "state_abbr": "TS", 
            "category_1": None, 
            "category_2": "http://existing.com",
            "lsc_grantee_profiles": ["http://existing.com"]
        },
        {
            "state_name": "AnotherState", 
            "state_abbr": "AS", 
            "category_1": "http://existing.com", 
            "category_2": "http://existing.com",
            "lsc_grantee_profiles": ["http://existing.com"]
        }
    ]
    
    with patch("builtins.open", mock_open(read_data=json.dumps(existing_data))):
        generate_state_resource_index(force_rebuild=False)

    assert mock_search.call_count == 1
    mock_search.assert_called_with("Search for TestState TS category 1")


@patch("os.path.exists", return_value=True)
@patch("scripts.state_data.extract_info.search_duckduckgo", return_value="http://rebuilt.com")
@patch("scripts.state_data.extract_info.extract_lsc_grantee_urls", return_value={
    "TestState": [{"profile_url": "http://mocked.com"}],
    "AnotherState": [{"profile_url": "http://mocked.com"}]
})
@patch("time.sleep")
def test_generate_index_force_rebuild(mock_sleep: MagicMock, mock_extract: MagicMock, mock_search: MagicMock, mock_exists: MagicMock, mock_constants: Any):
    """
    Given: The index file exists and is complete.
    When: generate_state_resource_index is called with force_rebuild=True.
    Then: It should ignore existing data and perform a full rebuild.
    """
    existing_data = [
        {
            "state_name": "TestState", 
            "state_abbr": "TS", 
            "category_1": "http://existing.com", 
            "category_2": "http://existing.com",
            "lsc_grantee_profiles": ["http://existing.com"]
        }
    ]
    
    with patch("builtins.open", mock_open(read_data=json.dumps(existing_data))):
        generate_state_resource_index(force_rebuild=True)

    assert mock_search.call_count == 4


@patch("os.path.exists", return_value=True)
@patch("scripts.state_data.extract_info.search_duckduckgo", return_value="http://recovery.com")
@patch("scripts.state_data.extract_info.extract_lsc_grantee_urls", return_value={
    "TestState": [{"profile_url": "http://mocked.com"}],
    "AnotherState": [{"profile_url": "http://mocked.com"}]
})
@patch("time.sleep")
def test_generate_index_corrupt_json(mock_sleep: MagicMock, mock_extract: MagicMock, mock_search: MagicMock, mock_exists: MagicMock, mock_constants: Any):
    """
    Given: The index file exists but contains invalid JSON.
    When: generate_state_resource_index is called.
    Then: It should catch the exception, print a message, and perform a fresh build.
    """
    with patch("builtins.open", mock_open(read_data="NOT_JSON_DATA")):
        result = generate_state_resource_index(force_rebuild=False)

    assert len(result) == 2
    assert mock_search.call_count == 4


@pytest.fixture
def mock_instructor_deps() -> Generator[Dict[str, MagicMock], None, None]:
    """
    Mocks the OpenAI client and the Instructor factory to prevent 
    actual network initialization.
    """
    with patch("scripts.state_data.extract_info.AsyncOpenAI") as mock_openai_class, \
         patch("scripts.state_data.extract_info.instructor.from_openai") as mock_from_openai, \
         patch("scripts.state_data.extract_info.LOCAL_SERVER_URL", "http://localhost:8000"):
        yield {
            "openai_class": mock_openai_class,
            "from_openai": mock_from_openai
        }

def test_get_local_instructor_client_success(mock_instructor_deps: dict[str, MagicMock]):
    """
    Given: The local server URL and required constants.
    When: get_local_instructor_client() is called.
    Then: It should initialize AsyncOpenAI with the correct URL/Key 
          and return an Instructor client in MD_JSON mode.
    """
    mock_client_instance = MagicMock()
    mock_instructor_deps["from_openai"].return_value = mock_client_instance
    
    result = get_local_instructor_client()

    mock_instructor_deps["openai_class"].assert_called_once_with(
        base_url="http://localhost:8000",
        api_key="local_api_key"
    )

    mock_instructor_deps["from_openai"].assert_called_once()
    args, kwargs = mock_instructor_deps["from_openai"].call_args
    
    assert args[0] == mock_instructor_deps["openai_class"].return_value
    assert kwargs["mode"] is not None 
    
    assert result == mock_client_instance

def test_get_local_instructor_client_exception(mock_instructor_deps: dict[str, MagicMock]):
    """
    Given: A failure in the Instructor library's factory method.
    When: get_local_instructor_client() is called.
    Then: The exception should propagate correctly.
    """
    mock_instructor_deps["from_openai"].side_effect = ValueError("Invalid Configuration")

    with pytest.raises(ValueError, match="Invalid Configuration"):
        get_local_instructor_client()


@pytest.mark.parametrize("input_url, expected", [
# Positive cases
("https://example.com", "https://example.com"),
("  https://example.com  ", "https://example.com"),
("https:\\\\example.com", "https://example.com"),
("https\u2044example.com", "https/example.com"),
("https\u2215example.com", "https/example.com"),
("https⁄example.com", "https/example.com"),
# Edge cases
("", ""),
(None, ""),
("   ", ""),
# Complex cases
("https\u2044\\⁄example.com", "https///example.com"),
("Multiple\u2044Slashes\\In\\Url", "Multiple/Slashes/In/Url"),
])
def test_sanitize_url_scenarios(input_url: str, expected: str):
    """
    Test the sanitize_url function with various inputs including 
    standard URLs, URLs with special slashes, and empty/None values.
    """
    # Given
    raw_url = input_url
    # When
    result = sanitize_url(raw_url)
    # Then
    assert result == expected

@pytest.fixture
def mock_session() -> Generator[requests.Session, None, None]:
    """Fixture to mock the requests.Session object."""
    with patch("requests.Session") as mock_session_class:
        mock_instance = cast(requests.Session, mock_session_class.return_value)
        yield mock_instance

def test_extract_lsc_grantee_urls_success_with_redirects(mock_session: MagicMock):
    """
    Given: A valid HTML page with accordion items and a grantee link that redirects.
    When: extract_lsc_grantee_urls is called with resolve_redirects=True.
    Then: It should return the correctly parsed grantees with resolved profile URLs.
    """
    # Mock HTML content
    html_content = """
    <div class="paragraph--type--accordion-item">
        <div class="field--name-field-plain-title">Alabama</div>
        <div class="field--name-field-long-description">
            <a href="https://example.com/grantee1">Grantee One</a>
            <a href="https://example.com/state-totals">State Totals</a>
        </div>
    </div>
    """
    
    # Mock initial response
    mock_initial_res = MagicMock()
    mock_initial_res.text = html_content
    mock_initial_res.status_code = 200
    
    # Mock redirect response
    mock_redirect_res = MagicMock()
    mock_redirect_res.url = "https://example.com/grantee1/final-path"
    mock_redirect_res.status_code = 200

    # Configure mock sequence
    # First call is for the target_url, second is for the redirect
    mock_session.get.side_effect = [mock_initial_res, mock_redirect_res]

    result = extract_lsc_grantee_urls("https://www.lsc.gov/about-lsc/our-grantees")

    # Assertions
    assert "Alabama" in result
    grantees = result["Alabama"]
    assert len(grantees) == 1
    assert grantees[0]["grantee_name"] == "Grantee One"
    assert grantees[0]["node_url"] == "https://example.com/grantee1"
    assert grantees[0]["profile_url"] == "https://example.com/grantee1/final-path"
    
    # Ensure "State Totals" was skipped
    assert len(grantees) == 1 

def test_extract_lsc_grantee_urls_no_redirects(mock_session: MagicMock):
    """
    Given: A valid HTML page and resolve_redirects=False.
    When: extract_lsc_grantee_urls is called.
    Then: It should return the joined URLs without attempting to resolve redirects.
    """
    html_content = """
    <div class="paragraph--type--accordion-item">
        <div class="field--name-field-plain-title">Alabama</div>
        <div class="field--name-field-long-description">
            <a href="https://example.com/grantee1">Grantee One</a>
        </div>
    </div>
    """
    mock_initial_res = MagicMock()
    mock_initial_res.text = html_content
    mock_initial_res.status_code = 200
    mock_session.get.return_value = mock_initial_res

    result = extract_lsc_grantee_urls("https://www.lsc.gov/about-lsc/our-grantees", resolve_redirects=False)

    assert "Alabama" in result
    grantees = result["Alabama"]
    assert grantees[0]["profile_url"] == "https://example.com/grantee1"
    # Verify session.get was only called once (for the main page)
    assert mock_session.get.call_count == 1

def test_extract_lsc_grantee_urls_redirect_failure(mock_session: MagicMock):
    """
    Given: A valid HTML page, but the redirect request fails.
    When: extract_lsc_grantee_urls is called.
    Then: It should catch the exception, print an error, and keep the original node_url.
    """
    html_content = """
    <div class="paragraph--type--accordion-item">
        <div class="field--name-field-plain-title">Alabama</div>
        <div class="field--name-field-long-description">
            <a href="https://example.com/grantee1">Grantee One</a>
        </div>
    </div>
    """
    mock_initial_res = MagicMock()
    mock_initial_res.text = html_content
    mock_initial_res.status_code = 200
    
    # First call success, second call raises exception
    mock_session.get.side_effect = [mock_initial_res, requests.exceptions.RequestException("Timeout")]

    result = extract_lsc_grantee_urls("https://www.lsc.gov/about-lsc/our-grantees")

    assert "Alabama" in result
    grantees = result["Alabama"]
    # Should fall back to the joined node_url
    assert grantees[0]["profile_url"] == "https://example.com/grantee1"

def test_extract_lsc_grantee_urls_empty_or_malformed_page(mock_session: MagicMock):
    """
    Given: An HTML page with no accordion items or missing titles.
    When: extract_lsc_grantee_urls is called.
    Then: It should return an empty dictionary.
    """
    html_content = "<html><body>No accordions here</body></html>"
    mock_initial_res = MagicMock()
    mock_initial_res.text = html_content
    mock_initial_res.status_code = 200
    mock_session.get.return_value = mock_initial_res

    result = extract_lsc_grantee_urls("https://www.lsc.gov/about-lsc/our-grantees")

    assert result == {}

def test_extract_lsc_grantee_urls_initial_request_failure(mock_session: MagicMock):
    """
    Given: The initial request to the LSC page fails.
    When: extract_lsc_grantee_urls is called.
    Then: It should raise a requests.exceptions.HTTPError (via raise_for_status).
    """
    mock_initial_res = MagicMock()
    mock_initial_res.raise_for_status.side_effect = requests.exceptions.HTTPError("404 Not Found")
    mock_session.get.return_value = mock_initial_res

    with pytest.raises(requests.exceptions.HTTPError):
        extract_lsc_grantee_urls("https://www.lsc.gov/invalid-page")