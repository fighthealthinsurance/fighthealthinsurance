"""A .scroll-x box scrolls sideways and never up and down.

The review page (categorize_review) wraps its form table in one, and now and
then showed a second, inner vertical scrollbar beside the page's own.
overflow-x:auto on its own computes overflow-y to auto too, so anything that
reached a pixel past the box's bottom (a password manager's badge dropped into
a field, a sub-pixel rounding at some zooms) gave the box a scrollbar of its
own. The box is exactly as tall as what it holds, so it has nothing to scroll
to vertically.
"""

import re

from django.test import SimpleTestCase

from tests.sync.test_readable_type import MAIN_CSS, rule


class ScrollXBoxTest(SimpleTestCase):
    def test_scroll_x_box_scrolls_sideways(self):
        _sel, body = rule(MAIN_CSS, r"(^|\n)\.scroll-x\s*$")
        self.assertRegex(body, r"overflow-x:\s*auto;")

    def test_scroll_x_box_never_scrolls_vertically(self):
        _sel, body = rule(MAIN_CSS, r"(^|\n)\.scroll-x\s*$")
        overflow_y = re.search(r"overflow-y:\s*([^;]+);", body)
        assert overflow_y is not None, ".scroll-x leaves overflow-y to compute as auto"
        self.assertEqual(overflow_y.group(1).strip(), "hidden")
