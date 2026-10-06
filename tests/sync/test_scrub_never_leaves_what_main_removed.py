"""Neither scrubber leaves a personal detail that main's scrubber took out.

Remove personal details (scrub_scrub.ts) and the chat's scrubPersonalInfo
(user_info_storage.ts) used to find what the person typed anywhere in the
text. Now a value of one word is found only where it stands whole, and a
value of more words wherever main found it, together with the rest of the
word its first or last word runs on into ("283 24th Street" for a typed
"283 24th St"). That is meant to leave exactly one thing that matching
anywhere took out, a match strictly inside one word like the "ann" in
"annual", and to take out exactly one thing main kept, the rest of a word a
value of more words runs on into. Two reviews found places where the
whole-word version left a name, an email, a street or a ZIP code in the
letter that matching anywhere had removed, so this checks the rule itself
rather than a list of examples.

Main's three scrubber files are kept, byte for byte, in
tests/js/scrub_before_whole_words (from 1a24d583, the commit this work
started from), so the comparison still means something once this is on
main. Both are compiled with the repo's tsc and run in node over the same
generated corpus (tests/js/scrub_differential.cjs), over several seeds:
names short and long, accented, hyphenated, with apostrophes and in several
parts, initials with a period, Chinese, Japanese and Korean names (with
particles joined on), Arabic names (with prefixes joined on, and inside
Arabic words), Latin names and emails right against Chinese and Arabic
text, emails with more after them, streets, names and emails printed
longer than typed at either end, streets with commas standing on their
own, ZIP and ZIP+4 codes with spaces, tabs and non-breaking spaces, upper
and lower case, possessives, letters that already hold placeholders, text
in double braces that is not a placeholder, and greeting and Patient:
lines with text run on after them. Remove personal details runs over every
input of the rendered intake page.

What is checked, for both scrubbers (the driver's header has the detail):
- every letter, digit or mark main took out is gone, unless the letters,
  digits and marks of main's match all lie in one word of the letter that
  goes on past them;
- every character main kept that the branch took out lies in a match of a
  typed value found the way main found it, not strictly inside one word, or
  in the rest of the word a value of more words runs on into, and nothing
  past it; and every word the typed values took anything out of is taken
  out whole;
- no placeholder of the site, one in the letter or one put in, is written
  into, and what the typed values put in is site placeholders only.

Skipped, not silently passed, where node or the front-end toolchain is not
installed. Every name, address, email and number is made up.
"""

import json
import os
import pathlib
import shutil
import subprocess

import pytest
from bs4 import BeautifulSoup
from django.test import TestCase
from django.urls import reverse

from tests.sync.test_entity_fetcher_behaviour import NODE, needs_node
from tests.sync.test_scrub_whole_words_behaviour import (
    JS,
    REPO_ROOT,
    compile_typescript,
)

MAIN_SOURCES = REPO_ROOT / "tests" / "js" / "scrub_before_whole_words"
DRIVER = REPO_ROOT / "tests" / "js" / "scrub_differential.cjs"
SCRUBBERS = ("typed_value_pattern.ts", "scrub_scrub.ts", "user_info_storage.ts")
# Generated cases per seed, after the hand-written ones from the reviews.
GENERATED = 25000
SEEDS = (20261006, 1, 2, 3)
# Each kind of text the corpus is meant to have, with how many cases of it
# there are at the least.
KINDS = (
    "chinese or japanese text",
    "korean text",
    "latin beside cjk",
    "email inside cjk",
    "email with dotted continuation",
    "nbsp",
    "tab",
    "existing placeholder",
    "lower case possessive",
    "upper case possessive",
    "greeting",
    "patient label before unspaced prose",
    "standalone comma",
    "zip+4 with odd spacing",
    "upper case",
    "one character first name",
    "long first name",
    "accented name",
    "apostrophe surname",
    "hyphenated surname",
    "multi-part surname",
    "cjk name",
    "typed zip+4",
    "arabic text",
    "latin beside arabic",
    "email beside arabic",
    "initial with a period",
    "korean name with a particle",
    "arabic name with a prefix",
    "arabic name inside an arabic word",
    "name in braces that are not a placeholder",
    "street runs on",
    "email runs on",
    "full name runs on",
)
# For each seed.
AT_LEAST = 100

LEFT = "left what main took out"
TOOK_OUT = "took out text main kept"
PART_OF_A_WORD = "took out part of a word"
PLACEHOLDER_KINDS = (
    "wrote into a placeholder",
    "put in something other than a placeholder",
    "left a broken placeholder",
    "took out part of a placeholder in the letter",
)


@pytest.fixture(scope="module")
def builds(tmp_path_factory) -> dict:
    """Main's scrubbers and this branch's, each compiled the way the bundle
    is built. Main's compile beside a stand-in for shared.ts (see the note in
    that file)."""
    out = tmp_path_factory.mktemp("scrub-differential")
    branch = out / "branch"
    compile_typescript(branch, [JS / name for name in SCRUBBERS], cwd=JS)
    source = out / "main-source"
    shutil.copytree(MAIN_SOURCES, source)
    main = out / "main"
    compile_typescript(
        main, [source / name for name in (*SCRUBBERS, "shared.ts")], cwd=source
    )
    return {"main": main, "branch": branch}


@pytest.fixture(scope="class")
def builds_for_class(request, builds):
    request.cls.builds = builds


# One run of the driver per scrubber compared, shared by the tests below.
REPORTS: dict = {}


@needs_node
@pytest.mark.usefixtures("builds_for_class")
class NeverLeavesWhatMainRemovedTest(TestCase):
    builds: dict

    def report(
        self,
        branch: str = "branch",
        count: int = GENERATED,
        seeds: tuple = SEEDS,
    ) -> dict:
        key = (branch, count, seeds)
        if key not in REPORTS:
            html = self.client.get(reverse("scan")).content.decode()
            soup = BeautifulSoup(html, "html.parser")
            inputs = [
                {
                    "id": tag.get("id") or "",
                    "type": tag.get("type") or "text",
                    "value": tag.get("value") or "",
                }
                for tag in soup.find_all("input")
            ]
            spec = {
                "main": str(self.builds["main"]),
                "branch": (
                    str(self.builds["branch"]) if branch == "branch" else branch
                ),
                "inputs": inputs,
                "count": count,
                "seeds": list(seeds),
            }
            result = subprocess.run(
                [NODE, str(DRIVER), json.dumps(spec)],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=1200,
                env=dict(os.environ, NODE_ENV="test"),
            )
            if result.returncode != 0:
                pytest.fail(
                    f"the differential run crashed\nstdout:\n{result.stdout}\n"
                    f"stderr:\n{result.stderr}"
                )
            REPORTS[key] = json.loads(result.stdout)
        return REPORTS[key]

    def assert_no(self, scrubber: str, *kinds: str) -> None:
        found = {
            kind: details
            for kind, details in self.report()[scrubber]["violations"].items()
            if kind in kinds
        }
        self.assertEqual(
            found, {}, json.dumps(found, ensure_ascii=False, indent=1)[:20000]
        )

    def test_the_corpus_is_large_and_has_every_kind_of_text(self):
        report = self.report()
        self.assertGreaterEqual(report["cases"], GENERATED * len(SEEDS))
        for seed in SEEDS:
            for kind in KINDS:
                with self.subTest(seed=seed, kind=kind):
                    self.assertGreaterEqual(
                        report["kindsBySeed"][str(seed)].get(kind, 0), AT_LEAST
                    )
        for scrubber in ("form", "chat"):
            with self.subTest(scrubber=scrubber):
                counts = report[scrubber]
                self.assertGreater(counts["charactersChecked"], 100000)
                self.assertGreater(counts["charactersMainKeptChecked"], 100000)
                # Each allowance is used, so a pass is not the corpus never
                # reaching it.
                self.assertGreater(counts["mainMatchesInsideAWord"], 1000)
                self.assertGreater(counts["runOnCharactersTakenOut"], 10000)
                self.assertGreater(counts["wordsTakenOutByTypedValues"], 100000)

    def test_remove_personal_details_leaves_nothing_main_took_out(self):
        self.assert_no("form", LEFT)

    def test_the_chat_leaves_nothing_main_took_out(self):
        self.assert_no("chat", LEFT)

    def test_remove_personal_details_takes_out_no_text_main_kept(self):
        self.assert_no("form", TOOK_OUT)

    def test_the_chat_takes_out_no_text_main_kept(self):
        self.assert_no("chat", TOOK_OUT)

    def test_remove_personal_details_takes_out_whole_words_only(self):
        self.assert_no("form", PART_OF_A_WORD)

    def test_the_chat_takes_out_whole_words_only(self):
        self.assert_no("chat", PART_OF_A_WORD)

    def test_remove_personal_details_writes_into_no_placeholder(self):
        self.assert_no("form", *PLACEHOLDER_KINDS)

    def test_the_chat_writes_into_no_placeholder(self):
        self.assert_no("chat", *PLACEHOLDER_KINDS)

    def test_the_check_fails_a_scrubber_that_takes_nothing_out(self):
        """So a pass above is not the check finding nothing to check."""
        report = self.report("identity", 500, SEEDS[:1])
        for scrubber in ("form", "chat"):
            with self.subTest(scrubber=scrubber):
                self.assertGreater(report[scrubber]["violations"][LEFT]["count"], 0)

    def test_the_check_passes_main_against_itself(self):
        """Main writes into its own placeholders (a last name "Name" into
        {{FIRST_NAME}}) and takes parts of words out ("ann" from "annual"),
        which are what is fixed, so only the comparisons with main are
        expected to pass."""
        report = self.report("main", 2000, SEEDS[:1])
        for scrubber in ("form", "chat"):
            with self.subTest(scrubber=scrubber):
                violations = report[scrubber]["violations"]
                self.assertNotIn(LEFT, violations)
                self.assertNotIn(TOOK_OUT, violations)

    def test_the_check_fails_main_for_taking_out_part_of_a_word(self):
        """So a pass of the whole-word check above is not the check finding
        nothing to check."""
        report = self.report("main", 2000, SEEDS[:1])
        for scrubber in ("form", "chat"):
            with self.subTest(scrubber=scrubber):
                violations = report[scrubber]["violations"]
                self.assertGreater(violations[PART_OF_A_WORD]["count"], 0)
