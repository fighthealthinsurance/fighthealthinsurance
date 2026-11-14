"""Blog posts and pages read well outside the site: share previews, BlogPosting
JSON-LD, the RSS feed, sitemap dates, and a real 404 for a post that isn't
there."""

import json
from html import unescape
import re
from datetime import date
from xml.etree import ElementTree

from django.test import TestCase
from django.urls import reverse

from fighthealthinsurance import blog
from fighthealthinsurance.agent_docs import _blog_posts
from fighthealthinsurance.sitemap import BlogSitemap

SHARE_IMAGE = "https://www.fighthealthinsurance.com/static/images/og-share.jpg"


def _meta(html: str, attr: str, name: str) -> str:
    match = re.search(
        rf'<meta {attr}="{re.escape(name)}"\s+content="([^"]*)"', html
    )
    assert match, f"no {name} tag"
    return unescape(match.group(1))


def _json_ld_nodes(html: str) -> list:
    nodes = []
    for raw in re.findall(
        r'<script type="application/ld\+json">(.*?)</script>', html, re.S
    ):
        data = json.loads(raw)
        nodes.extend(data.get("@graph", [data]) if isinstance(data, dict) else data)
    return nodes


class BlogPostPageTest(TestCase):
    def setUp(self):
        self.post = _blog_posts()[0]
        self.url = reverse("blog-post", kwargs={"slug": self.post["slug"]})

    def test_unknown_slug_is_a_404(self):
        response = self.client.get(
            reverse("blog-post", kwargs={"slug": "no-such-post-here"})
        )
        self.assertEqual(response.status_code, 404)

    def test_real_post_still_renders(self):
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_post_shares_as_an_article_with_its_title(self):
        html = self.client.get(self.url).content.decode()
        self.assertEqual(_meta(html, "property", "og:type"), "article")
        self.assertIn(self.post["title"], _meta(html, "property", "og:title"))
        self.assertEqual(
            _meta(html, "property", "og:url"),
            "https://www.fighthealthinsurance.com" + self.url,
        )

    def test_post_has_blog_posting_json_ld(self):
        html = self.client.get(self.url).content.decode()
        postings = [n for n in _json_ld_nodes(html) if n.get("@type") == "BlogPosting"]
        self.assertEqual(len(postings), 1)
        posting = postings[0]
        self.assertEqual(posting["headline"], self.post["title"])
        self.assertEqual(posting["datePublished"], self.post["date"][:10])
        self.assertEqual(
            posting["publisher"],
            {"@id": "https://www.fighthealthinsurance.com/#organization"},
        )
        self.assertEqual(
            posting["mainEntityOfPage"]["@id"],
            "https://www.fighthealthinsurance.com" + self.url,
        )


class SharePreviewTagsTest(TestCase):
    def test_home_page_has_share_tags(self):
        html = self.client.get(reverse("root")).content.decode()
        self.assertEqual(_meta(html, "property", "og:type"), "website")
        self.assertEqual(_meta(html, "property", "og:image"), SHARE_IMAGE)
        self.assertEqual(_meta(html, "name", "twitter:card"), "summary_large_image")
        self.assertEqual(_meta(html, "name", "twitter:image"), SHARE_IMAGE)
        self.assertTrue(_meta(html, "property", "og:title"))

    def test_page_title_and_description_reach_the_share_tags(self):
        html = self.client.get(reverse("blog")).content.decode()
        title = unescape(re.search(r"<title>(.*?)</title>", html, re.S).group(1))
        self.assertEqual(_meta(html, "property", "og:title"), title)
        self.assertEqual(
            _meta(html, "property", "og:description"),
            _meta(html, "name", "description"),
        )
        self.assertTrue(_meta(html, "name", "description"))

    def test_pages_link_the_feed(self):
        html = self.client.get(reverse("root")).content.decode()
        self.assertIn(
            f'<link rel="alternate" type="application/rss+xml" '
            f'title="Fight Health Insurance blog" href="{reverse("blog-feed")}">',
            html,
        )


class BlogFeedTest(TestCase):
    def test_feed_is_rss_not_a_blog_post(self):
        self.assertEqual(reverse("blog-feed"), "/blog/feed/")
        response = self.client.get("/blog/feed/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("application/rss+xml", response["Content-Type"])

    def test_feed_lists_every_post_newest_first(self):
        root = ElementTree.fromstring(self.client.get("/blog/feed/").content)
        links = [item.findtext("link") for item in root.iter("item")]
        expected = sorted(_blog_posts(), key=lambda p: p.get("date", ""), reverse=True)
        self.assertEqual(links, [blog.post_url(p["slug"]) for p in expected])
        first = next(root.iter("item"))
        self.assertTrue(first.findtext("title"))
        self.assertTrue(first.findtext("pubDate"))


class BlogDatesTest(TestCase):
    def test_sitemap_lastmod_is_the_post_date(self):
        self.assertEqual(
            BlogSitemap().lastmod({"slug": "x", "date": "2025-07-15"}),
            date(2025, 7, 15),
        )

    def test_sitemap_lastmod_prefers_updated(self):
        self.assertEqual(
            BlogSitemap().lastmod(
                {"slug": "x", "date": "2025-07-15", "updated": "2026-01-02"}
            ),
            date(2026, 1, 2),
        )

    def test_no_date_means_no_lastmod(self):
        self.assertIsNone(BlogSitemap().lastmod({"slug": "x", "date": ""}))

    def test_date_modified_only_when_updated_is_set(self):
        plain = blog.blog_posting_json_ld({"date": "2025-07-15"}, "x")
        updated = blog.blog_posting_json_ld(
            {"date": "2025-07-15", "updated": "2026-01-02"}, "x"
        )
        self.assertNotIn("dateModified", plain)
        self.assertEqual(updated["dateModified"], "2026-01-02")

    def test_author_byline_becomes_a_person(self):
        node = blog.blog_posting_json_ld(
            {"author": "[Henry Wu](https://example.com/hw) | Policy Analyst"}, "x"
        )
        self.assertEqual(
            node["author"],
            {"@type": "Person", "name": "Henry Wu", "url": "https://example.com/hw"},
        )

    def test_team_byline_credits_the_organization(self):
        node = blog.blog_posting_json_ld({"author": "FHI Team"}, "x")
        self.assertEqual(
            node["author"], {"@id": "https://www.fighthealthinsurance.com/#organization"}
        )


class FrontMatterFallbackTest(TestCase):
    """Without blog_posts.json, front matter is read as YAML like the generator."""

    def test_quoted_values_lose_their_quotes(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from fighthealthinsurance import agent_docs

        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "a-post.md").write_text(
                "---\ntitle: 'A post'\ndate: 2026-01-01\n"
                "updated: '2026-01-02'\nauthor: 'FHI Team'\n---\n\nBody.\n",
                encoding="utf-8",
            )
            with patch.object(agent_docs, "_static_path", return_value=tmp):
                posts = agent_docs._blog_posts_from_sources()
        self.assertEqual(
            posts,
            [
                {
                    "title": "A post",
                    "date": "2026-01-01",
                    "updated": "2026-01-02",
                    "author": "FHI Team",
                    "slug": "a-post",
                }
            ],
        )


class MetaTitleTest(TestCase):
    def test_blog_post_meta_title_is_not_empty(self):
        from fighthealthinsurance.agent_docs import _blog_posts as posts

        slug = posts()[0]["slug"]
        html = self.client.get(
            reverse("blog-post", kwargs={"slug": slug})
        ).content.decode()
        self.assertTrue(_meta(html, "name", "title").strip())
