"""Tests for the per-state external review & Department of Insurance complaint
guidance feature.

These tests exercise the shipped ``state_help.json`` data directly (rather than
through ``staticfiles_storage``, which depends on ``collectstatic``) so they
validate the real data for all 50 states + DC, and they drive the
``StateHelpView`` with that real data to confirm external-review content
renders, unknown states 404, and there are no dangling internal links.
"""

import json
import os

from unittest.mock import patch

from django.conf import settings
from django.test import TestCase, Client
from django.urls import reverse, NoReverseMatch

from fighthealthinsurance.state_help import (
    StateHelp,
    ExternalReviewInfo,
    LAST_REVIEWED,
    VALID_EXTERNAL_REVIEW_TYPES,
    EXTERNAL_REVIEW_TYPE_LABELS,
)

# The canonical set of slugs we require: all 50 states plus the District of
# Columbia (51 total). Kept explicit so a dropped/renamed state fails loudly.
EXPECTED_STATE_SLUGS = {
    "alabama",
    "alaska",
    "arizona",
    "arkansas",
    "california",
    "colorado",
    "connecticut",
    "delaware",
    "district-of-columbia",
    "florida",
    "georgia",
    "hawaii",
    "idaho",
    "illinois",
    "indiana",
    "iowa",
    "kansas",
    "kentucky",
    "louisiana",
    "maine",
    "maryland",
    "massachusetts",
    "michigan",
    "minnesota",
    "mississippi",
    "missouri",
    "montana",
    "nebraska",
    "nevada",
    "new-hampshire",
    "new-jersey",
    "new-mexico",
    "new-york",
    "north-carolina",
    "north-dakota",
    "ohio",
    "oklahoma",
    "oregon",
    "pennsylvania",
    "rhode-island",
    "south-carolina",
    "south-dakota",
    "tennessee",
    "texas",
    "utah",
    "vermont",
    "virginia",
    "washington",
    "west-virginia",
    "wisconsin",
    "wyoming",
}

# Internal routes the state page links to. If any of these stop resolving the
# per-state pages would contain dangling links, so we assert they all reverse.
INTERNAL_LINK_ROUTE_NAMES = [
    "scan",
    "chat",
    "other-resources",
    "state_help_index",
]


def _state_help_json_path() -> str:
    """Absolute path to the shipped state_help.json in the app static dir."""
    app_static = getattr(
        settings,
        "APP_STATIC_DIR",
        os.path.join(settings.BASE_DIR, "fighthealthinsurance", "static"),
    )
    return os.path.join(app_static, "state_help.json")


def _load_raw_state_data() -> dict:
    """Load the raw shipped state help JSON (excluding the 'national' entry)."""
    with open(_state_help_json_path(), "r", encoding="utf-8") as f:
        data = json.load(f)
    return {slug: v for slug, v in data.items() if slug != "national"}


def _build_state_help_map() -> dict:
    """Build a {slug: StateHelp} map from the real shipped data."""
    return {slug: StateHelp(v) for slug, v in _load_raw_state_data().items()}


class ExternalReviewDataIntegrityTest(TestCase):
    """Validate the shipped per-state external-review data."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.raw = _load_raw_state_data()
        cls.states = _build_state_help_map()

    def test_all_fifty_one_states_present(self):
        """All 50 states + DC are present, and nothing extra."""
        self.assertEqual(set(self.states.keys()), EXPECTED_STATE_SLUGS)
        self.assertEqual(len(self.states), 51)

    def test_every_state_parses_into_state_help(self):
        """Every shipped entry validates as a StateHelp without error."""
        for slug, state in self.states.items():
            self.assertEqual(state.slug, slug)
            self.assertTrue(state.name)
            self.assertTrue(state.abbreviation)
            self.assertTrue(
                state.insurance_department.name,
                msg=f"{slug} missing insurance department name",
            )

    def test_every_state_has_external_review_with_valid_type(self):
        """Each state exposes external review info with a recognized review_type."""
        for slug, state in self.states.items():
            self.assertIsNotNone(
                state.external_review, msg=f"{slug} missing external_review"
            )
            self.assertIn(
                state.external_review.review_type,
                VALID_EXTERNAL_REVIEW_TYPES,
                msg=f"{slug} has invalid review_type",
            )
            # A human-readable label must exist for whatever type is set.
            self.assertIn(
                state.external_review.review_type_label,
                EXTERNAL_REVIEW_TYPE_LABELS.values(),
            )

    def test_no_dangling_external_review_links(self):
        """External review info_urls, where present, are absolute http(s) URLs."""
        for slug, state in self.states.items():
            info_url = state.external_review.info_url
            self.assertTrue(info_url, msg=f"{slug} external_review missing info_url")
            self.assertTrue(
                info_url.startswith("http://") or info_url.startswith("https://"),
                msg=f"{slug} external_review info_url is not absolute: {info_url}",
            )

    def test_internal_link_routes_resolve(self):
        """The internal routes the page links to all reverse (no dangling links)."""
        for name in INTERNAL_LINK_ROUTE_NAMES:
            try:
                reverse(name)
            except NoReverseMatch:  # pragma: no cover - failure path
                self.fail(f"Internal link route {name!r} does not resolve")

    def test_last_reviewed_is_iso_date(self):
        """LAST_REVIEWED is a plausible ISO (YYYY-MM-DD) date, not fabricated text."""
        import datetime

        # Raises ValueError (failing the test) if not a valid ISO date.
        datetime.date.fromisoformat(LAST_REVIEWED)


class ExternalReviewInfoTypeTest(TestCase):
    """Unit tests for the extended ExternalReviewInfo type handling."""

    def test_valid_review_type_preserved(self):
        info = ExternalReviewInfo(
            {"available": True, "info_url": "https://x", "review_type": "federal"}
        )
        self.assertEqual(info.review_type, "federal")
        self.assertFalse(info.is_state_administered)

    def test_state_type_is_state_administered(self):
        info = ExternalReviewInfo({"available": True, "review_type": "state"})
        self.assertTrue(info.is_state_administered)

    def test_unknown_review_type_falls_back_to_varies(self):
        """An unrecognized review_type is normalized to 'varies', never invented."""
        info = ExternalReviewInfo({"available": True, "review_type": "bogus"})
        self.assertEqual(info.review_type, "varies")

    def test_missing_review_type_defaults_to_varies(self):
        info = ExternalReviewInfo({"available": True})
        self.assertEqual(info.review_type, "varies")


class StateExternalReviewViewTest(TestCase):
    """Drive StateHelpView with the real shipped data."""

    def setUp(self):
        self.client = Client()
        self.state_map = _build_state_help_map()

    def _patched_get_state_help(self):
        """Patch get_state_help to resolve against the real shipped data map."""
        return patch(
            "fighthealthinsurance.state_help.get_state_help",
            side_effect=lambda slug: self.state_map.get(slug),
        )

    def test_all_states_resolve_with_200(self):
        """Every one of the 51 state slugs resolves to a 200 page."""
        with self._patched_get_state_help():
            for slug in sorted(EXPECTED_STATE_SLUGS):
                url = reverse("state_help", kwargs={"slug": slug})
                response = self.client.get(url)
                self.assertEqual(
                    response.status_code,
                    200,
                    msg=f"State {slug} did not return 200",
                )

    def test_sample_state_renders_external_review_content(self):
        """A sample state page renders the external-review & complaint guidance."""
        with self._patched_get_state_help():
            url = reverse("state_help", kwargs={"slug": "california"})
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        # Core external-review guidance is present.
        self.assertIn("External Review", content)
        self.assertIn("independent external review", content)
        # Reflects the federal HHS process (some plans use it).
        self.assertIn("Department of Health", content)
        # Names the state Department of Insurance for complaints.
        self.assertIn("California Department of Insurance", content)
        # General deadlines note (no fabricated specific deadline).
        self.assertIn("Deadlines vary", content)
        # Safety disclaimer + last reviewed date.
        self.assertIn("not legal advice", content)
        self.assertIn(LAST_REVIEWED, content)

    def test_sample_state_links_to_internal_appeal_flow(self):
        """The page links to the appeal flow and internal appeal resources."""
        with self._patched_get_state_help():
            url = reverse("state_help", kwargs={"slug": "texas"})
            response = self.client.get(url)
        content = response.content.decode("utf-8")
        self.assertIn(reverse("scan"), content)
        self.assertIn(reverse("other-resources"), content)
        self.assertIn(reverse("state_help_index"), content)

    def test_sample_state_has_seo_structured_data_and_canonical(self):
        """The page emits WebPage + BreadcrumbList JSON-LD and a canonical link."""
        with self._patched_get_state_help():
            url = reverse("state_help", kwargs={"slug": "new-york"})
            response = self.client.get(url)
        content = response.content.decode("utf-8")
        self.assertIn("application/ld+json", content)
        self.assertIn("BreadcrumbList", content)
        self.assertIn("WebPage", content)
        self.assertIn('rel="canonical"', content)

        # The structured data in context must be valid JSON with both nodes.
        structured = response.context["structured_data_json"]
        parsed = json.loads(structured)
        types = {node["@type"] for node in parsed["@graph"]}
        self.assertEqual(types, {"WebPage", "BreadcrumbList"})

    def test_unknown_state_returns_404(self):
        """An unknown state slug returns a 404."""
        with self._patched_get_state_help():
            url = reverse("state_help", kwargs={"slug": "not-a-real-state"})
            response = self.client.get(url)
        self.assertEqual(response.status_code, 404)
