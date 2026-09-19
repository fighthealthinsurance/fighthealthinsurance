"""JSON-LD goes on a page through one hardened renderer, or not at all.

A string value carrying ``</script>`` must not be able to close the element
it sits in. Taken from #903 with the glossary, trimmed to the renderer, since
the site's Organization and WebSite nodes come from an existing partial that
this does not touch.
"""

import json

from django.test import TestCase

from fighthealthinsurance import structured_data


class RenderJsonLdTest(TestCase):
    def test_it_is_a_json_ld_script_tag(self):
        html = structured_data.render_json_ld({"@type": "Thing", "name": "x"})

        self.assertTrue(html.startswith('<script type="application/ld+json">'))
        self.assertTrue(html.endswith("</script>"))

    def test_a_value_cannot_close_the_script(self):
        html = structured_data.render_json_ld(
            {"@type": "Thing", "name": "</script><img src=x onerror=alert(1)>"}
        )

        self.assertNotIn("</script><img", html)
        self.assertIn("\\u003c/script\\u003e", html)
        self.assertEqual(html.count("</script>"), 1)

    def test_an_ampersand_cannot_start_an_entity(self):
        html = structured_data.render_json_ld({"name": "Blue Cross &amp; Blue Shield"})

        self.assertNotIn("&amp;", html)
        self.assertIn("\\u0026", html)

    def test_a_list_of_nodes_is_valid_too(self):
        html = structured_data.render_json_ld([{"@type": "A"}, {"@type": "B"}])

        payload = html[len('<script type="application/ld+json">') : -len("</script>")]
        self.assertEqual(json.loads(payload), [{"@type": "A"}, {"@type": "B"}])

    def test_what_comes_back_survives_a_round_trip(self):
        data = {"@type": "DefinedTerm", "name": "Prior Authorization"}

        html = structured_data.render_json_ld(data)

        payload = html[len('<script type="application/ld+json">') : -len("</script>")]
        self.assertEqual(json.loads(payload), data)
