"""Every comment in the site's stylesheets closes before the next one opens.

A comment ended with "*" instead of "*/" runs on to the next comment's end,
and the browser silently drops every rule in between. Nothing else fails:
the page just loses styles. A "/*" inside a comment is the sign of it.
"""

import pathlib

import pytest

CSS_DIR = pathlib.Path(__file__).resolve().parents[2] / "fighthealthinsurance" / "static" / "css"
OURS = ("custom.css", "main.css")


def _unclosed(css: str) -> list[int]:
    """Line numbers of comments that open another comment or never close."""
    problems, i = [], 0
    while (start := css.find("/*", i)) != -1:
        end = css.find("*/", start + 2)
        line = css.count("\n", 0, start) + 1
        if end == -1 or "/*" in css[start + 2 : end]:
            problems.append(line)
            if end == -1:
                break
        i = end + 2
    return problems


@pytest.mark.parametrize("name", OURS)
def test_every_comment_closes(name):
    assert _unclosed((CSS_DIR / name).read_text()) == [], (
        f"{name}: a comment starting on these lines swallows the rules after it"
    )


def test_the_check_finds_a_comment_left_open():
    assert _unclosed("/* one *\n.a { color: red; }\n/* two */") == [1]
    assert _unclosed("/* one */ .a {} /* two */") == []
