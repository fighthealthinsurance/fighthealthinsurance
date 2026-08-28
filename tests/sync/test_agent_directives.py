"""Tests for the agent-readable site: llms.txt, robots.txt, markdown twins
(<page>.md) and the <head> directives that advertise them."""

from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse

from fighthealthinsurance import agent_docs


class TestPathHelpers(TestCase):
    def test_twin_path_round_trips(self):
        for source, twin in [
            ("/", "/index.md"),
            ("/about", "/about.md"),
            ("/blog/", "/blog/index.md"),
            ("/blog/some-post/", "/blog/some-post/index.md"),
        ]:
            self.assertEqual(agent_docs.twin_path_for(source), twin)
            self.assertEqual(agent_docs.source_path_for(twin), source)

    def test_eligibility_is_public_content_only(self):
        self.assertTrue(agent_docs.twin_eligible("about"))
        self.assertTrue(agent_docs.twin_eligible("blog-post"))
        self.assertTrue(agent_docs.twin_eligible("state_help"))
        self.assertTrue(agent_docs.twin_eligible("state_help_index"))
        self.assertTrue(agent_docs.twin_eligible("microsite"))
        self.assertFalse(agent_docs.twin_eligible("scan"))
        self.assertFalse(agent_docs.twin_eligible("pro_version"))
        self.assertFalse(agent_docs.twin_eligible("admin_status"))
        self.assertFalse(agent_docs.twin_eligible(None))


_SAMPLE_HTML = """
<html><head><title>Sample :: FHI</title>
<meta name="description" content="A short description.">
<script>var x = 1;</script></head>
<body><nav><a href="/">Nav link</a></nav>
<main id="main-content">
  <h1>Heading</h1>
  <p>Some <strong>bold</strong> text and a <a href="/about-us">relative link</a>.</p>
  <ul><li>One</li><li>Two <em>italic</em></li></ul>
  <form><input name="x"><button>Send</button></form>
  <img src="/x.png" alt="pic">
  <table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>
</main>
<footer>Footer stuff</footer></body></html>
"""
_SAMPLE_URL = "https://www.fighthealthinsurance.com/sample"


class TestHtmlToMarkdown(TestCase):
    def setUp(self):
        self.md = agent_docs.html_to_markdown(_SAMPLE_HTML, _SAMPLE_URL)

    def test_header_carries_description_and_url(self):
        self.assertIn("> A short description.", self.md)
        self.assertIn(f"- URL: {_SAMPLE_URL}", self.md)

    def test_headings_and_inline_formatting(self):
        self.assertIn("# Heading", self.md)
        self.assertIn("Some **bold** text", self.md)
        self.assertIn("Two *italic*", self.md)

    def test_relative_links_become_absolute(self):
        self.assertIn(
            "[relative link](https://www.fighthealthinsurance.com/about-us)", self.md
        )

    def test_lists(self):
        self.assertIn("- One\n- Two *italic*", self.md)

    def test_tables(self):
        self.assertIn("| A | B |", self.md)
        self.assertIn("| 1 | 2 |", self.md)

    def test_chrome_scripts_forms_and_images_are_dropped(self):
        for absent in ("Nav link", "Footer stuff", "Send", "var x", "<", "pic"):
            self.assertNotIn(absent, self.md)

    def test_uses_title_when_page_has_no_h1(self):
        html = "<html><head><title>Only Title</title></head><body><main><p>Body.</p></main></body></html>"
        md = agent_docs.html_to_markdown(html, "https://example.test/p")
        self.assertTrue(md.startswith("# Only Title\n"))


class TestAgentEndpoints(TestCase):
    """The public pages are whole-response cached (StaticIshView), so clear
    the cache on both sides: no stale page from an earlier test, and none
    left behind for a later one."""

    def setUp(self):
        cache.clear()
        self.client = Client()

    def tearDown(self):
        cache.clear()

    def test_llms_txt(self):
        response = self.client.get(reverse("llms_txt"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/markdown; charset=utf-8")
        body = response.content.decode("utf-8")
        self.assertTrue(body.startswith("# Fight Health Insurance\n"))
        about_twin = agent_docs.twin_path_for(reverse("about"))
        self.assertIn(f"https://www.fighthealthinsurance.com{about_twin}", body)
        self.assertIn("https://www.fighthealthinsurance.com/index.md", body)
        self.assertIn("## Blog", body)
        self.assertIn("/blog/", body)
        self.assertIn("public", response["Cache-Control"])

    def test_robots_txt(self):
        response = self.client.get("/robots.txt")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("text/plain"))
        body = response.content.decode("utf-8")
        self.assertIn("User-agent: *", body)
        self.assertIn("Content-Signal: search=yes, ai-input=yes", body)
        self.assertIn("Sitemap: http://testserver/sitemap.xml", body)

    def test_markdown_twin_of_static_page(self):
        about = reverse("about")
        response = self.client.get(agent_docs.twin_path_for(about))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/markdown; charset=utf-8")
        self.assertEqual(response["Link"], '</llms.txt>; rel="describedby"')
        body = response.content.decode("utf-8")
        self.assertTrue(body.startswith("# "))
        self.assertIn(f"- URL: https://www.fighthealthinsurance.com{about}", body)
        self.assertIn(
            "- Site index for agents: https://www.fighthealthinsurance.com/llms.txt",
            body,
        )
        self.assertNotIn("<script", body)
        self.assertNotIn("<div", body)

    def test_markdown_twin_of_root(self):
        response = self.client.get("/index.md")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/markdown; charset=utf-8")
        self.assertIn(
            "- URL: https://www.fighthealthinsurance.com/",
            response.content.decode("utf-8"),
        )

    def test_markdown_twins_of_index_pages(self):
        for name in ("state_help_index", "microsite_directory", "blog", "faq"):
            twin = agent_docs.twin_path_for(reverse(name))
            response = self.client.get(twin)
            self.assertEqual(response.status_code, 200, twin)
            self.assertEqual(response["Content-Type"], "text/markdown; charset=utf-8")

    def test_markdown_twin_of_blog_post_is_the_source(self):
        posts = agent_docs._blog_posts()
        self.assertTrue(
            posts, "blog_posts.json should be findable without collectstatic"
        )
        slug = posts[0]["slug"]
        twin = agent_docs.twin_path_for(reverse("blog-post", kwargs={"slug": slug}))
        self.assertTrue(twin.endswith("/index.md"))
        response = self.client.get(twin)
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertTrue(
            body.startswith("---\n"), body[:80]
        )  # front matter from the .md source
        self.assertIn(f'slug: "{slug}"', body)
        self.assertIn("- Site index for agents:", body)

    def test_ineligible_pages_have_no_twin(self):
        for path in [
            "/.md",
            "/pro_version.md",
            "/scan.md",
            "/timbit/help.md",
            "/nope.md",
            "/index.md.md",
        ]:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 404, path)

    def test_head_advertises_twin_index_and_schema(self):
        response = self.client.get(reverse("about"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        twin = agent_docs.twin_path_for(reverse("about"))
        self.assertIn(
            f'<link rel="alternate" type="text/markdown" href="{twin}">', html
        )
        self.assertIn('<link rel="describedby" href="/llms.txt">', html)
        self.assertIn('"@type": "Organization"', html)
        self.assertIn('"@type": "WebSite"', html)

    def test_ineligible_page_advertises_index_but_no_twin(self):
        response = self.client.get(reverse("pro_version"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        self.assertIn('<link rel="describedby" href="/llms.txt">', html)
        self.assertNotIn('type="text/markdown"', html)
