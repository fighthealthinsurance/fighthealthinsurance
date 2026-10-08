"""The privacy policy and the MHMDA notice are printed to PDF, and their
print rules hide the site header by its class. When the header lost
Bootstrap's .navbar, those rules matched nothing and the PDF opened with the
logo and the Menu toggle above the title."""

import pathlib
import re

TEMPLATES = pathlib.Path(__file__).resolve().parents[2] / "fighthealthinsurance" / "templates"
HEADER = re.search(
    r'<section class="([^"]*)" role="navigation">', (TEMPLATES / "base.html").read_text()
)


def _print_hidden_classes(name: str) -> set[str]:
    text = (TEMPLATES / name).read_text()
    block = re.search(r"@media print\s*\{(.*?)\{\s*display:\s*none", text, re.S)
    assert block, f"{name} has no print rule hiding the page's chrome"
    return set(re.findall(r"\.([\w-]+)", block.group(1)))


def test_the_header_has_a_class_to_hide_by() -> None:
    assert HEADER, "base.html's header section moved; update this test"


def test_the_printed_privacy_policy_hides_the_header() -> None:
    assert set(HEADER.group(1).split()) & _print_hidden_classes("privacy_policy.html")


def test_the_printed_mhmda_notice_hides_the_header() -> None:
    assert set(HEADER.group(1).split()) & _print_hidden_classes("mhmda.html")
