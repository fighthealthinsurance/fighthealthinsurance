"""Agent-readable twins of the public site (llms.txt v2).

Humans get HTML. Agents get the same page as markdown at ``<url>.md``
(directory-style URLs use ``<url>index.md``), advertised from every eligible
page's ``<head>`` via ``<link rel="alternate" type="text/markdown">``, and a
site index at ``/llms.txt`` advertised via ``<link rel="describedby">``.

Only public content pages are eligible: the static pages the sitemap already
advertises, blog posts, state help pages and microsites. Everything in the
appeal flow, the staff area and the API is refused with a 404 before the
underlying view is ever called.

Same-URL content negotiation (``Accept: text/markdown``) is deliberately not
done here. The site sits behind Cloudflare's cache, which keys on the URL and
does not honor ``Vary: Accept``, so a markdown response could be cached and
handed to a browser. Distinct ``.md`` URLs cannot collide that way.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from django.contrib.staticfiles import finders
from django.contrib.staticfiles.storage import staticfiles_storage
from django.http import Http404, HttpRequest, HttpResponse
from django.urls import Resolver404, resolve, reverse
from django.views import View
from django.views.decorators.cache import cache_control, cache_page

from bs4 import BeautifulSoup, NavigableString, Tag
from loguru import logger

from fighthealthinsurance.sitemap import StaticViewSitemap

CANONICAL_ORIGIN = "https://www.fighthealthinsurance.com"
MARKDOWN_CONTENT_TYPE = "text/markdown; charset=utf-8"

# Public content pages that are not in the static sitemap list but still
# belong in the agent-readable set. Parameterised names (blog-post, state_help,
# microsite) cover every instance of that route.
EXTRA_TWIN_URL_NAMES = frozenset(
    {
        "about-ai",
        "how-to-help",
        "blog-post",
        "state_help_index",
        "state_help",
        "smtp-domain-faq",
        "microsite",
    }
)

# Short notes for the llms.txt page list, keyed by URL name. Pages without an
# entry are listed by URL alone.
PAGE_NOTES: dict[str, tuple[str, str]] = {
    "root": (
        "Home",
        "what the tool does and how to start an appeal from a photo of the denial",
    ),
    "about": ("About us", "who makes this and why it exists"),
    "about-ai": ("About our AI", "how the models are used and what they cannot do"),
    "how-to-help": ("How to help", "ways to support the project"),
    "faq": ("FAQ", "common questions about appeals and about using the tool"),
    "medicaid-faq": ("Medicaid work requirements FAQ", ""),
    "smtp-domain-faq": (
        "Email domain FAQ",
        "why our emails come from the domains they do",
    ),
    "denial-language-library": (
        "Denial language library",
        "common phrases insurers use in denials and what they mean",
    ),
    "preparing-2026": ("Preparing for 2026", "insurance changes to plan for"),
    "turning-26": ("Turning 26", "coverage options when you age off a parent's plan"),
    "medicaid-eligibility": ("Medicaid eligibility", ""),
    "other-resources": ("Other resources", "organizations and tools beyond this site"),
    "pbs-newshour": ("As seen on PBS NewsHour", ""),
    "media-references": ("Media references", "press coverage"),
    "blog": ("Blog", "index of posts"),
    "microsite_directory": (
        "Condition and procedure guides",
        "index of the microsites",
    ),
    "state_help_index": (
        "Help by state",
        "index of state-level regulators and appeal rights",
    ),
    "contact": ("Contact", ""),
    "tos": ("Terms of service", ""),
    "privacy_policy": ("Privacy policy", ""),
    "mhmda": ("Washington MHMDA notice", "consumer health data disclosures"),
}

_STRIP_TAGS = (
    "script",
    "style",
    "noscript",
    "nav",
    "form",
    "button",
    "input",
    "select",
    "textarea",
    "svg",
    "iframe",
    "img",
    "video",
    "audio",
    "canvas",
    "template",
)
_INLINE_TAGS = frozenset(
    {
        "a",
        "strong",
        "b",
        "em",
        "i",
        "code",
        "span",
        "small",
        "sup",
        "sub",
        "abbr",
        "u",
        "s",
    }
)
_HEADING = re.compile(r"^h([1-6])$")


def static_twin_url_names() -> frozenset[str]:
    """URL names eligible for a markdown twin."""
    return frozenset(StaticViewSitemap().items()) | EXTRA_TWIN_URL_NAMES


def twin_eligible(url_name: Optional[str]) -> bool:
    return bool(url_name) and url_name in static_twin_url_names()


def twin_path_for(path: str) -> str:
    """Markdown twin path for a page path: ``/about`` -> ``/about.md``,
    ``/`` -> ``/index.md``, ``/blog/x/`` -> ``/blog/x/index.md``."""
    if path.endswith("/"):
        return path + "index.md"
    return path + ".md"


def source_path_for(twin_path: str) -> str:
    """Inverse of :func:`twin_path_for`."""
    if not twin_path.endswith(".md"):
        raise ValueError(twin_path)
    base = twin_path[: -len(".md")]
    if base == "/index" or base.endswith("/index"):
        return base[: -len("index")]
    return base


# ---------------------------------------------------------------------------
# HTML -> markdown
# ---------------------------------------------------------------------------


def _absolute(href: str, origin: str) -> str:
    if href.startswith(("http://", "https://", "mailto:", "tel:")):
        return href
    if href.startswith("/"):
        return origin + href
    return f"{origin}/{href}"


def _text(node: NavigableString) -> str:
    return re.sub(r"\s+", " ", str(node))


def _render(node: Any, origin: str, list_depth: int = 0) -> str:
    """Render a BeautifulSoup node to markdown. Block elements own their own
    surrounding blank lines; inline elements return bare text."""
    if isinstance(node, NavigableString):
        if type(node) is not NavigableString:  # comments, CDATA, doctype
            return ""
        return _text(node)
    if not isinstance(node, Tag):
        return ""
    name = node.name.lower()
    if name in _STRIP_TAGS or node.get("aria-hidden") == "true":
        return ""
    if name == "br":
        return "\n"
    if name == "hr":
        return "\n\n---\n\n"
    if name == "pre":
        return "\n\n```\n" + str(node.get_text()).strip("\n") + "\n```\n\n"

    children = "".join(_render(c, origin, list_depth) for c in node.children)
    heading = _HEADING.match(name)
    if heading:
        text = children.strip()
        return f"\n\n{'#' * int(heading.group(1))} {text}\n\n" if text else ""
    if name == "p":
        return "\n\n" + children.strip() + "\n\n"
    if name == "blockquote":
        body = children.strip()
        return "\n\n" + "\n".join("> " + line for line in body.splitlines()) + "\n\n"
    if name in ("ul", "ol"):
        items = []
        for i, li in enumerate(node.find_all("li", recursive=False), start=1):
            marker = f"{i}." if name == "ol" else "-"
            body = _render_children(li, origin, list_depth + 1).strip()
            body = body.replace("\n\n", "\n").replace(
                "\n", "\n" + "  " * (list_depth + 1)
            )
            items.append("  " * list_depth + f"{marker} {body}")
        return "\n\n" + "\n".join(items) + "\n\n"
    if name == "li":
        return children
    if name == "a":
        text = children.strip()
        href = node.get("href")
        if not text:
            return ""
        if not isinstance(href, str) or href.startswith(("#", "javascript:")):
            return text
        return f"[{text}]({_absolute(href, origin)})"
    if name in ("strong", "b"):
        text = children.strip()
        return f"**{text}**" if text else ""
    if name in ("em", "i"):
        text = children.strip()
        return f"*{text}*" if text else ""
    if name == "code":
        return f"`{children.strip()}`"
    if name == "table":
        return _render_table(node, origin)
    if name in _INLINE_TAGS:
        return children
    # Generic block container (div, section, article, header, footer, ...):
    # separate from neighbours so adjacent text does not run together.
    return "\n\n" + children + "\n\n"


def _render_children(node: Tag, origin: str, list_depth: int) -> str:
    return "".join(_render(c, origin, list_depth) for c in node.children)


def _render_table(table: Tag, origin: str) -> str:
    rows = []
    for tr in table.find_all("tr"):
        cells = [
            " ".join(_render_children(td, origin, 0).split())
            for td in tr.find_all(["th", "td"], recursive=False)
        ]
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n\n" + "\n".join(out) + "\n\n"


def _tidy(markdown: str) -> str:
    markdown = re.sub(r"[ \t]+\n", "\n", markdown)
    markdown = re.sub(r"\n[ \t]+(?=\S)", "\n", markdown)
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)
    return markdown.strip() + "\n"


def html_to_markdown(html: str, page_url: str, origin: str = CANONICAL_ORIGIN) -> str:
    """Convert a rendered page to markdown: the ``<main>`` content only, with
    a small header (title, description, URL, site index) on top."""
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    title = re.sub(r"\s+", " ", title)
    description = ""
    meta = soup.find("meta", attrs={"name": "description"})
    if isinstance(meta, Tag):
        content = meta.get("content")
        if isinstance(content, str):
            description = re.sub(r"\s+", " ", content).strip()

    main = soup.find("main") or soup.body or soup
    body = _tidy(_render(main, origin))

    head: list[str] = []
    if title and not body.startswith("# "):
        head += [f"# {title}", ""]
    if description:
        head += [f"> {description}", ""]
    head += [
        f"- URL: {page_url}",
        f"- Site index for agents: {origin}{reverse('llms_txt')}",
        "",
    ]
    return "\n".join(head) + "\n" + body


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


def _static_path(name: str) -> Optional[str]:
    """Locate a static file: the collected STATIC_ROOT in production, else the
    app's own static dir via the finders (dev and CI never run collectstatic)."""
    try:
        if staticfiles_storage.exists(name):
            return str(staticfiles_storage.path(name))
    except Exception as e:  # storage without a local path, etc.
        logger.debug(f"staticfiles_storage could not resolve {name}: {e}")
    found = finders.find(name)
    if isinstance(found, str):
        return found
    if isinstance(found, (list, tuple)) and found:
        return str(found[0])
    return None


def _read_static_text(name: str) -> Optional[str]:
    path = _static_path(name)
    if path is None:
        logger.warning(f"Static file {name} not found")
        return None
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        logger.warning(f"Could not read static file {name}: {e}")
        return None


class MarkdownTwinView(View):
    """Serve ``<page>.md`` for an eligible public page."""

    http_method_names = ["get", "head"]

    def get(self, request: HttpRequest, path: str) -> HttpResponse:
        if not path:  # "/.md": the root's twin is /index.md only
            raise Http404("No markdown twin for this page")
        source = source_path_for("/" + path + ".md")
        try:
            match = resolve(source)
        except Resolver404:
            raise Http404("No such page")
        if not twin_eligible(match.url_name):
            raise Http404("No markdown twin for this page")

        page_url = CANONICAL_ORIGIN + source
        markdown: Optional[str] = None
        if match.url_name == "blog-post":
            slug = match.kwargs.get("slug", "")
            if re.match(r"^[a-zA-Z0-9_-]+$", slug):
                # Blog posts are written in markdown; hand over the source.
                markdown = _read_static_text(f"blog/{slug}.md")
                if markdown is not None:
                    markdown = markdown.rstrip("\n") + (
                        f"\n\n- URL: {page_url}\n"
                        f"- Site index for agents: {CANONICAL_ORIGIN}{reverse('llms_txt')}\n"
                    )

        if markdown is None:
            request.resolver_match = match
            page = match.func(request, *match.args, **match.kwargs)
            if hasattr(page, "render") and callable(page.render):
                page = page.render()
            if page.status_code != 200 or not page.get("Content-Type", "").startswith(
                "text/html"
            ):
                raise Http404("Page did not render")
            markdown = html_to_markdown(page.content.decode("utf-8"), page_url)

        response = HttpResponse(markdown, content_type=MARKDOWN_CONTENT_TYPE)
        response["Link"] = f'<{reverse("llms_txt")}>; rel="describedby"'
        response["Cache-Control"] = "public, max-age=1800"
        return response


_FRONT_MATTER_LINE = re.compile(r'^([A-Za-z_]+):\s*"?(.*?)"?\s*$')


def _blog_posts_from_sources() -> list[dict[str, Any]]:
    """Post list read straight from the blog/*.md front matter, newest first.
    blog_posts.json (what the site itself uses) is generated from these files
    and is not checked in, so it is absent on a fresh checkout and in CI."""
    blog_dir = _static_path("blog")
    if blog_dir is None or not Path(blog_dir).is_dir():
        return []
    posts: list[dict[str, Any]] = []
    for md in sorted(Path(blog_dir).glob("*.md")):
        try:
            text = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not text.startswith("---"):
            continue
        end = text.find("\n---", 3)
        if end < 0:
            continue
        meta: dict[str, Any] = {}
        for line in text[3:end].splitlines():
            m = _FRONT_MATTER_LINE.match(line.strip())
            if m and m.group(1) in ("title", "slug", "date", "description", "excerpt"):
                meta[m.group(1)] = m.group(2)
        meta.setdefault("slug", md.stem)
        posts.append(meta)
    posts.sort(key=lambda p: str(p.get("date", "")), reverse=True)
    return posts


def _blog_posts() -> list[dict[str, Any]]:
    raw = _read_static_text("blog_posts.json")
    if raw:
        try:
            posts = json.loads(raw)
            if isinstance(posts, list):
                return [p for p in posts if isinstance(p, dict) and p.get("slug")]
        except json.JSONDecodeError as e:
            logger.warning(f"Could not parse blog_posts.json: {e}")
    return _blog_posts_from_sources()


def _twin_link(url_name: str, **kwargs: Any) -> str:
    return CANONICAL_ORIGIN + twin_path_for(reverse(url_name, kwargs=kwargs or None))


def build_llms_txt() -> str:
    lines = [
        "# Fight Health Insurance",
        "",
        "> Fight Health Insurance is a free tool that helps patients appeal health "
        "insurance denials. Take a picture of the denial letter and it drafts an "
        "appeal to submit, explains the denial, and points to the next steps and "
        "the regulators for your state. A professional version, Fight Paperwork, "
        "does the same for clinics and providers.",
        "",
        "Every public page has a markdown twin at the same URL plus `.md` "
        "(`/about` -> `/about.md`, `/` -> `/index.md`), also advertised on each "
        'page via `<link rel="alternate" type="text/markdown">`. Prefer the '
        "twins: same words, no navigation or scripts. Pages inside the appeal "
        "flow are personal to the person appealing and have no twins.",
        "",
        "## Pages",
        "",
    ]
    for name in StaticViewSitemap().items() + sorted(
        EXTRA_TWIN_URL_NAMES - {"blog-post", "state_help", "microsite"}
    ):
        try:
            url = _twin_link(name)
        except Exception:  # route not mounted in this deployment
            continue
        title, note = PAGE_NOTES.get(
            name, (name.replace("-", " ").replace("_", " ").title(), "")
        )
        lines.append(f"- [{title}]({url})" + (f": {note}" if note else ""))

    posts = _blog_posts()
    if posts:
        lines += ["", "## Blog", ""]
        for post in posts:
            url = _twin_link("blog-post", slug=post["slug"])
            date = post.get("date", "")
            desc = (post.get("description") or post.get("excerpt") or "").strip()
            tail = ". ".join(x for x in (date, desc) if x)
            lines.append(
                f"- [{post.get('title', post['slug'])}]({url})"
                + (f": {tail}" if tail else "")
            )

    try:
        from fighthealthinsurance.state_help import load_state_help

        states = load_state_help()
    except Exception as e:
        logger.warning(f"Could not load state help for llms.txt: {e}")
        states = {}
    if states:
        lines += ["", "## Help by state", ""]
        for slug, state in sorted(states.items(), key=lambda kv: kv[1].name):
            lines.append(f"- [{state.name}]({_twin_link('state_help', slug=slug)})")

    try:
        from fighthealthinsurance.microsites import get_all_microsites

        microsites = {
            slug: m
            for slug, m in get_all_microsites().items()
            if not getattr(m, "wip", False)
        }
    except Exception as e:
        logger.warning(f"Could not load microsites for llms.txt: {e}")
        microsites = {}
    if microsites:
        lines += ["", "## Condition and procedure guides", ""]
        for slug, m in sorted(microsites.items(), key=lambda kv: kv[1].title):
            lines.append(
                f"- [{m.title}]({_twin_link('microsite', slug=slug)}): {m.tagline}"
            )

    lines += [
        "",
        "## Optional",
        "",
        f"- [Sitemap]({CANONICAL_ORIGIN}{reverse('django.contrib.sitemaps.views.sitemap')})",
        "- [Source code](https://github.com/orgs/fighthealthinsurance/repositories): "
        "the tool is open source",
        "- [Substack](https://fighthealthinsurance.substack.com/): newsletter",
        "- [LinkedIn](https://www.linkedin.com/company/fight-health-insurance)",
        "- [YouTube](https://www.youtube.com/@fighthealthinsuranceyt)",
        "",
    ]
    return "\n".join(lines)


@cache_control(public=True)
@cache_page(60 * 60)
def llms_txt_view(request: HttpRequest) -> HttpResponse:
    """Site index for AI agents (llms.txt v2)."""
    return HttpResponse(build_llms_txt(), content_type=MARKDOWN_CONTENT_TYPE)


@cache_control(public=True)
@cache_page(60 * 60)
def robots_txt_view(request: HttpRequest) -> HttpResponse:
    """robots.txt. Cloudflare prepends its managed Content Signals block in
    front of whatever the origin serves; this is the origin's part."""
    sitemap = request.build_absolute_uri(
        reverse("django.contrib.sitemaps.views.sitemap")
    )
    body = "\n".join(
        [
            "User-agent: *",
            "Allow: /",
            "",
            "# Content Signals (https://contentsignals.org): search results and",
            "# AI answers that cite us are welcome. Training is left unspecified.",
            "Content-Signal: search=yes, ai-input=yes",
            "",
            f"Sitemap: {sitemap}",
            "",
        ]
    )
    return HttpResponse(body, content_type="text/plain; charset=utf-8")
