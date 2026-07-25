"""Tests for the reusable schema.org structured-data helpers.

Covers the typed builders (valid schema.org shape), the hardened render helper
(escapes ``</script>``), and the sitewide Organization + WebSite JSON-LD being
present in the base template.
"""

import json

from django.template.loader import render_to_string
from django.test import TestCase
from django.urls import reverse

from fighthealthinsurance import structured_data
from fighthealthinsurance.templatetags import structured_data_tags


class TestOrganizationBuilder(TestCase):
    """The Organization builder emits truthful first-party identity only."""

    def test_has_schema_context_and_type(self):
        org = structured_data.organization()
        self.assertEqual(org["@context"], "https://schema.org")
        self.assertEqual(org["@type"], "Organization")

    def test_uses_real_identity(self):
        org = structured_data.organization()
        self.assertEqual(org["name"], "Fight Health Insurance")
        self.assertEqual(org["url"], "https://www.fighthealthinsurance.com")
        self.assertTrue(org["logo"].startswith("https://www.fighthealthinsurance.com"))
        self.assertIn("sameAs", org)
        self.assertTrue(
            all(link.startswith("https://") for link in org["sameAs"]),
        )

    def test_never_emits_fabricated_ratings_or_awards(self):
        # Content-safety guard: no invented ratings/reviews/awards/stats.
        org = structured_data.organization()
        for forbidden in ("aggregateRating", "review", "award", "ratingValue"):
            self.assertNotIn(forbidden, org)


class TestWebSiteBuilder(TestCase):
    """WebSite builder, with the optional SearchAction."""

    def test_basic_shape_without_search(self):
        site = structured_data.website()
        self.assertEqual(site["@context"], "https://schema.org")
        self.assertEqual(site["@type"], "WebSite")
        self.assertEqual(site["url"], "https://www.fighthealthinsurance.com")
        # No search endpoint on this site -> no potentialAction.
        self.assertNotIn("potentialAction", site)

    def test_search_action_added_when_search_url_given(self):
        template_url = (
            "https://www.fighthealthinsurance.com/search?q={search_term_string}"
        )
        site = structured_data.website(search_url_template=template_url)
        action = site["potentialAction"]
        self.assertEqual(action["@type"], "SearchAction")
        self.assertEqual(action["target"]["urlTemplate"], template_url)
        self.assertEqual(action["query-input"], "required name=search_term_string")


class TestBreadcrumbBuilder(TestCase):
    def test_positions_and_items(self):
        crumbs = structured_data.breadcrumb_list(
            [
                ("Home", "https://www.fighthealthinsurance.com/"),
                ("FAQ", "https://www.fighthealthinsurance.com/faq"),
            ]
        )
        self.assertEqual(crumbs["@type"], "BreadcrumbList")
        elements = crumbs["itemListElement"]
        self.assertEqual([e["position"] for e in elements], [1, 2])
        self.assertEqual(elements[0]["name"], "Home")
        self.assertEqual(
            elements[1]["item"], "https://www.fighthealthinsurance.com/faq"
        )
        self.assertTrue(all(e["@type"] == "ListItem" for e in elements))

    def test_accepts_mapping_items_and_omits_missing_url(self):
        crumbs = structured_data.breadcrumb_list([{"name": "Current page"}])
        element = crumbs["itemListElement"][0]
        self.assertEqual(element["name"], "Current page")
        self.assertNotIn("item", element)


class TestFaqPageBuilder(TestCase):
    def test_questions_and_answers(self):
        faq = structured_data.faq_page(
            [
                ("Is it free?", "Yes, the core tools are free."),
                {"question": "How long?", "answer": "A few minutes."},
            ]
        )
        self.assertEqual(faq["@type"], "FAQPage")
        entities = faq["mainEntity"]
        self.assertEqual(len(entities), 2)
        self.assertEqual(entities[0]["@type"], "Question")
        self.assertEqual(entities[0]["name"], "Is it free?")
        answer = entities[0]["acceptedAnswer"]
        self.assertEqual(answer["@type"], "Answer")
        self.assertEqual(answer["text"], "Yes, the core tools are free.")
        self.assertEqual(entities[1]["name"], "How long?")


class TestHowToBuilder(TestCase):
    def test_steps_string_and_mapping(self):
        howto = structured_data.how_to(
            "Appeal a denial",
            steps=[
                "Upload your denial letter.",
                {
                    "name": "Review",
                    "text": "Review the generated appeal.",
                    "url": "https://x/2",
                },
            ],
            description="Steps to appeal.",
            total_time="PT15M",
        )
        self.assertEqual(howto["@type"], "HowTo")
        self.assertEqual(howto["name"], "Appeal a denial")
        self.assertEqual(howto["totalTime"], "PT15M")
        steps = howto["step"]
        self.assertEqual([s["position"] for s in steps], [1, 2])
        self.assertTrue(all(s["@type"] == "HowToStep" for s in steps))
        self.assertEqual(steps[0]["text"], "Upload your denial letter.")
        self.assertEqual(steps[1]["name"], "Review")
        self.assertEqual(steps[1]["url"], "https://x/2")


class TestWebPageBuilders(TestCase):
    def test_web_page_shape(self):
        page = structured_data.web_page(
            "About", url="https://x/about", description="About us."
        )
        self.assertEqual(page["@context"], "https://schema.org")
        self.assertEqual(page["@type"], "WebPage")
        self.assertEqual(page["name"], "About")
        self.assertEqual(page["url"], "https://x/about")
        self.assertEqual(page["description"], "About us.")

    def test_medical_web_page_shape_with_about_and_specialty(self):
        page = structured_data.medical_web_page(
            "Understanding denials",
            about="Health insurance denials",
            specialty="Insurance",
        )
        self.assertEqual(page["@type"], "MedicalWebPage")
        self.assertEqual(
            page["about"], {"@type": "Thing", "name": "Health insurance denials"}
        )
        self.assertEqual(page["specialty"], "Insurance")


class TestRenderJsonLd(TestCase):
    """The single hardened render path escapes script-breaking characters."""

    def test_wraps_in_ld_json_script_tag(self):
        html = structured_data.render_json_ld({"@type": "Thing", "name": "x"})
        self.assertTrue(html.startswith('<script type="application/ld+json">'))
        self.assertTrue(html.endswith("</script>"))

    def test_escapes_closing_script_tag(self):
        # A malicious/awkward string value must not be able to close the script.
        html = structured_data.render_json_ld(
            {"@type": "Thing", "name": "</script><img src=x onerror=alert(1)>"}
        )
        # The literal breakout sequence must not survive...
        self.assertNotIn("</script><img", html)
        # ...the '<' and '>' are escaped to their \uXXXX forms instead.
        self.assertIn("\\u003c/script\\u003e", html)
        self.assertIn("\\u003cimg", html)
        # Only the single trailing (real) closing tag remains.
        self.assertEqual(html.count("</script>"), 1)

    def test_escapes_ampersand(self):
        html = structured_data.render_json_ld({"name": "Tom & Jerry"})
        self.assertIn("\\u0026", html)
        self.assertNotIn("Tom & Jerry", html)

    def test_payload_is_still_valid_json_after_escaping(self):
        html = structured_data.render_json_ld({"name": "</script>"})
        inner = html[len('<script type="application/ld+json">') : -len("</script>")]
        # The browser JSON parser un-escapes \uXXXX, so the round trip holds.
        self.assertEqual(json.loads(inner)["name"], "</script>")

    def test_accepts_list_of_nodes_and_still_escapes(self):
        # Glossary/insurer/decoder pages pass a list of top-level JSON-LD nodes;
        # the result must be a single script tag wrapping a JSON array, with the
        # per-node escaping still applied.
        html = structured_data.render_json_ld(
            [
                {"@type": "DefinedTerm", "name": "A & B"},
                {"@type": "BreadcrumbList", "name": "</script>"},
            ]
        )
        self.assertTrue(html.startswith('<script type="application/ld+json">'))
        self.assertEqual(html.count("</script>"), 1)
        self.assertIn("\\u0026", html)
        self.assertIn("\\u003c/script\\u003e", html)
        inner = html[len('<script type="application/ld+json">') : -len("</script>")]
        parsed = json.loads(inner)
        self.assertEqual([node["@type"] for node in parsed], ["DefinedTerm", "BreadcrumbList"])
        self.assertEqual(parsed[0]["name"], "A & B")


class TestSitewideTemplateTag(TestCase):
    def test_tag_emits_both_organization_and_website(self):
        rendered = structured_data_tags.sitewide_structured_data()
        self.assertIn('"@type": "Organization"', rendered)
        self.assertIn('"@type": "WebSite"', rendered)
        self.assertEqual(rendered.count('type="application/ld+json"'), 2)


class TestBaseTemplateStructuredData(TestCase):
    """A rendered page (extending base.html) carries the sitewide JSON-LD."""

    def test_about_page_includes_organization_and_website_json_ld(self):
        response = self.client.get(reverse("about"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('type="application/ld+json"', content)
        self.assertIn('"@type": "Organization"', content)
        self.assertIn('"@type": "WebSite"', content)
        self.assertIn('"name": "Fight Health Insurance"', content)


class TestAiAnswerSummaryPartial(TestCase):
    """The opt-in AI-answer summary partial renders nothing unless used."""

    def test_renders_empty_without_summary_in_context(self):
        rendered = render_to_string("partials/ai_answer_summary.html", {})
        self.assertEqual(rendered.strip(), "")
