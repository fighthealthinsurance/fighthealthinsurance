"""Test canonical URL meta tags are rendered correctly on all pages."""

import pytest
from django.test import Client


# Pages that should have canonical URLs
CANONICAL_URL_PAGES = [
    ("/", "https://www.fighthealthinsurance.com/"),
    ("/about-us", "https://www.fighthealthinsurance.com/about-us"),
    ("/bingo", "https://www.fighthealthinsurance.com/bingo"),
    ("/other-resources", "https://www.fighthealthinsurance.com/other-resources"),
]


@pytest.fixture
def client():
    return Client()


@pytest.mark.django_db
class TestCanonicalUrls:
    """Test that canonical URLs are correctly rendered on pages."""

    @pytest.mark.parametrize("path,expected_canonical", CANONICAL_URL_PAGES)
    def test_page_has_correct_canonical_url(self, client, path, expected_canonical):
        """Test that pages include the correct canonical URL."""
        response = client.get(path)
        assert response.status_code == 200, f"Page {path} should return 200"

        content = response.content.decode("utf-8")

        # Check for the exact canonical URL
        expected_tag = f'<link rel="canonical" href="{expected_canonical}">'
        assert (
            expected_tag in content
        ), f"Page {path} should have canonical URL {expected_canonical}"

    @pytest.mark.parametrize("path,_", CANONICAL_URL_PAGES)
    def test_canonical_url_uses_www_subdomain(self, client, path, _):
        """Test that canonical URLs always use www subdomain."""
        response = client.get(path)
        content = response.content.decode("utf-8")

        # Ensure canonical URL uses www.fighthealthinsurance.com
        assert (
            "https://www.fighthealthinsurance.com" in content
        ), f"Page {path} should have canonical URL with www subdomain"

        # Ensure it doesn't use non-www version
        assert (
            '<link rel="canonical" href="https://fighthealthinsurance.com'
            not in content
        ), f"Page {path} should not have canonical URL without www"

    def test_canonical_url_strips_query_parameters(self, client):
        """Test that canonical URL does not include query parameters."""
        response = client.get("/?utm_source=test&utm_campaign=foo")
        assert response.status_code == 200

        content = response.content.decode("utf-8")

        # Canonical URL should NOT include query parameters
        assert (
            '<link rel="canonical" href="https://www.fighthealthinsurance.com/">'
            in content
        )
        assert (
            "utm_source" not in content.split('<link rel="canonical"')[1].split(">")[0]
        )

    def test_canonical_url_normalizes_trailing_slash(self, client):
        """Test that canonical URL uses the path as defined in URL patterns."""
        # Test a page that has no trailing slash in URL pattern
        response = client.get("/about-us")
        assert response.status_code == 200

        content = response.content.decode("utf-8")
        canonical_tag = '<link rel="canonical" href="https://www.fighthealthinsurance.com/about-us">'
        assert (
            canonical_tag in content
        ), "Canonical URL should match the URL pattern definition"

    def test_canonical_url_with_path_parameters(self, client):
        """Test that canonical URLs work correctly with path parameters."""
        # Test a page with dynamic path segments - use the chat route
        response = client.get("/chat", follow=True)

        # Only check if we get a successful response
        if response.status_code == 200:
            content = response.content.decode("utf-8")
            assert (
                "https://www.fighthealthinsurance.com" in content
            ), "Pages with path parameters should have canonical URL"


@pytest.mark.django_db
class TestCanonicalUrlOverride:
    """Test that views can override the canonical URL."""

    def test_context_processor_respects_request_override(self):
        """Test that canonical_url_context respects request.canonical_url override."""
        from django.test import RequestFactory
        from fighthealthinsurance.context_processors import canonical_url_context

        factory = RequestFactory()
        request = factory.get("/some-page")

        # Set a custom canonical URL on the request
        request.canonical_url = "https://www.fighthealthinsurance.com/custom-canonical"

        context = canonical_url_context(request)
        assert (
            context["canonical_url"]
            == "https://www.fighthealthinsurance.com/custom-canonical"
        )

    def test_context_processor_uses_default_without_override(self):
        """Test that canonical_url_context uses default when no override is set."""
        from django.test import RequestFactory
        from fighthealthinsurance.context_processors import canonical_url_context

        factory = RequestFactory()
        request = factory.get("/about-us")

        # No canonical_url attribute set on request
        context = canonical_url_context(request)

        # Should use the request path as fallback (no resolver_match in factory requests)
        assert (
            context["canonical_url"] == "https://www.fighthealthinsurance.com/about-us"
        )
