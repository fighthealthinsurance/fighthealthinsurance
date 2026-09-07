"""Django's ``{# #}`` comment is single-line only. Multi-line ones RENDER.

``django.template.base.tag_re`` matches ``{#.*?#}`` without ``re.DOTALL``, so a
comment that wraps onto a second line is never tokenized as a comment. The
lexer hands the whole thing back as text and the template prints it verbatim --
implementation notes, rationale, the lot -- in the middle of the page.

That is exactly what happened on the scrub page: the note explaining why the
file input deliberately carries no ``name=`` attribute was sitting under
"Attach all insurance denial pages below" for every user to read, and the
accessibility tree offered it up as the file input's description.

Multi-line explanations belong in ``{% comment %}...{% endcomment %}``.
"""

import pathlib
import re

from django.test import Client, TestCase
from django.urls import reverse

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# Third-party packages ship Jinja2 templates, where a multi-line ``{# #}`` is
# perfectly legal. Only OUR templates are bound by Django's stricter rule.
_NOT_OURS = {".venv", ".tox", "node_modules", "site-packages", ".git", "venv"}

_COMMENT_OPEN = "{#"
_COMMENT_CLOSE = "#}"

# Inside {% verbatim %} Django deliberately prints template syntax as-is, so a
# ``{# #}`` there is content that is meant to show, not a comment gone wrong.
# Inside {% comment %} nothing renders at all, so a multi-line ``{# #}`` there
# is harmless too (CodeRabbit on #994).
_VERBATIM = re.compile(
    r"{%\s*verbatim(?:\s+\w+)?\s*%}.*?{%\s*endverbatim(?:\s+\w+)?\s*%}",
    re.DOTALL,
)
_BLOCK_COMMENT = re.compile(
    r"{%\s*comment(?:\s+\S+)?\s*%}.*?{%\s*endcomment\s*%}", re.DOTALL
)


def first_party_templates():
    """Every file under a ``templates/`` tree we own, whatever its extension.

    Not only ``.html``: the patient-facing emails are ``.txt`` templates put
    through the same engine (``fighthealthinsurance/utils.py``), and a comment
    leaking into one of those goes out in someone's inbox.
    """
    for directory in REPO_ROOT.rglob("templates"):
        if not directory.is_dir():
            continue
        if _NOT_OURS & set(directory.relative_to(REPO_ROOT).parts):
            continue
        for path in directory.rglob("*"):
            if path.is_file():
                yield path


def _blank_verbatim_regions(source: str) -> str:
    """Drop verbatim and block-comment regions, keeping their newlines so
    line numbers hold."""
    keep_lines = lambda m: "\n" * m.group(0).count("\n")
    return _BLOCK_COMMENT.sub(keep_lines, _VERBATIM.sub(keep_lines, source))


def _multiline_comments(source: str):
    """Yield (line number, excerpt) for every ``{# #}`` that spans a newline."""
    source = _blank_verbatim_regions(source)
    index = 0
    while True:
        start = source.find(_COMMENT_OPEN, index)
        if start == -1:
            return
        end = source.find(_COMMENT_CLOSE, start)
        if end == -1:
            # Unclosed is worse than multi-line: everything after it renders.
            yield source.count("\n", 0, start) + 1, source[start : start + 60]
            return
        body = source[start + len(_COMMENT_OPEN) : end]
        if "\n" in body:
            yield source.count("\n", 0, start) + 1, body.strip()[:60]
        index = end + len(_COMMENT_CLOSE)


class MultilineTemplateCommentTest(TestCase):
    """No template may carry a comment that Django will print."""

    def test_no_multiline_hash_comments_in_any_template(self):
        templates = list(first_party_templates())
        # A silent zero-file scan would pass forever; make the walk prove itself.
        self.assertIn(
            REPO_ROOT / "fighthealthinsurance" / "templates" / "scrub.html",
            templates,
        )

        offenders = []
        for path in templates:
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue  # images and the like live under templates/ too
            for line, excerpt in _multiline_comments(source):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{line}: {excerpt!r}")
        self.assertEqual(
            offenders,
            [],
            "Multi-line {# #} comments render as visible page text. "
            "Use {% comment %}...{% endcomment %}:\n" + "\n".join(offenders),
        )

    def test_scanner_finds_a_multiline_comment(self):
        found = list(_multiline_comments("a\n{# two\nlines #}\nb"))
        self.assertEqual([(2, "two\nlines")], found)

    def test_scanner_reports_an_unclosed_comment(self):
        self.assertEqual(1, len(list(_multiline_comments("x {# never closed\n"))))

    def test_scanner_ignores_block_comments(self):
        # A {# #} inside {% comment %} never renders, whatever its shape.
        inside = "{% comment %}\n{# two\nlines #}\n{% endcomment %}\nx {# fine #}"
        self.assertEqual([], list(_multiline_comments(inside)))

    def test_scanner_ignores_verbatim_blocks(self):
        shown_on_purpose = "{% verbatim %}\n{# this is\ncontent #}\n{% endverbatim %}"
        self.assertEqual([], list(_multiline_comments(shown_on_purpose)))

    def test_django_really_does_render_a_multiline_hash_comment(self):
        """Pin the behaviour this test file exists for, so it cannot rot.

        If a future Django makes ``{# #}`` multi-line aware this fails, and the
        rule above can be relaxed deliberately rather than by accident.
        """
        from django.template import Context, Template

        rendered = Template("A {# one\nline #} B").render(Context({}))
        self.assertIn("line #}", rendered)


class ScrubPageCopyTest(TestCase):
    """The upload page shows copy, not the reasoning behind the markup."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def test_scan_page_does_not_leak_the_uploader_comment(self):
        response = Client().get(reverse("scan"))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")

        # The comment is gone...
        self.assertNotIn("DELIBERATELY", body)
        self.assertNotIn(_COMMENT_OPEN, body)

        # ...and the thing it was explaining is still true: no name=, in any
        # spelling, or the browser puts the document in the POST body.
        uploader = re.search(
            r"<input\b[^>]*\bid\s*=\s*[\"']uploader[\"'][^>]*>", body, re.IGNORECASE
        )
        self.assertIsNotNone(uploader, "the file input disappeared")
        assert uploader is not None  # for mypy
        self.assertIsNone(
            re.search(r"\bname\s*=", uploader.group(0), re.IGNORECASE),
            "the uploader grew a name= attribute: " + uploader.group(0),
        )
