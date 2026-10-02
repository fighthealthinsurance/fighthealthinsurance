"""A blog or FAQ post never makes the page wider than a phone.

The posts are markdown that blog_post.tsx turns into HTML in the browser, so
no template holds their tables or their text for the shell's checks to read.
The KFF premium table in the unaffordable-coverage post is six columns of
figures, 416px wide, and on a 390px phone it pushed the whole page 42px
sideways. blog_post.tsx now puts every table it renders in a .scroll-x box,
so a wide table scrolls inside that box, and .md-content lets a long word
break. These read the two files that do that, so the fix cannot quietly go
missing. They do not measure a rendered post: no Selenium test opens one yet.
"""

import re
from pathlib import Path

from tests.sync.test_shared_shell import CUSTOM_CSS, css_rules, declared

BLOG_POST_TSX = (
    Path(__file__).resolve().parents[2]
    / "fighthealthinsurance"
    / "static"
    / "js"
    / "blog_post.tsx"
)


def _table_wrapper(source: str):
    """The name of the top-level function that boxes each table, or None.

    A top-level function here starts at a line beginning ``const name =`` and
    ends at the first line that is just ``};``.
    """
    for match in re.finditer(r"^const (\w+) = .*?^\};", source, re.M | re.S):
        body = match.group(0)
        if re.search(r"querySelectorAll\(\s*['\"]table['\"]", body) and (
            "scroll-x" in body
        ):
            return match.group(1)
    return None


def test_blog_post_tsx_has_a_function_that_boxes_each_table() -> None:
    assert _table_wrapper(BLOG_POST_TSX.read_text()), (
        "blog_post.tsx no longer puts a post's tables in a .scroll-x box, so a "
        "table wider than the phone makes the whole page scroll sideways"
    )


def test_every_part_of_a_post_goes_through_the_table_box() -> None:
    """The post's body and the HTML block some posts open with."""
    source = BLOG_POST_TSX.read_text()
    wrapper = _table_wrapper(source) or "wrapTablesToScroll"
    for setter in ("setContent", "setLeadingContent"):
        assert re.search(rf"{setter}\(\s*{wrapper}\(", source), (
            f"{setter} is given HTML that has not been through {wrapper}, so "
            "a table in that part of a post can push the page sideways"
        )


def test_a_long_word_in_a_post_breaks() -> None:
    rule = dict(css_rules(CUSTOM_CSS)).get(".md-content")
    assert rule is not None, "custom.css has no .md-content rule"
    # break-word, not anywhere: anywhere also shrinks a table's columns to a
    # character each, so the table would split every figure to fit the phone
    # rather than scroll in its box.
    assert declared(rule, "overflow-wrap") == "break-word", (
        "a word with no space in it, in a post, runs past a phone's edge and "
        "takes the page with it"
    )
