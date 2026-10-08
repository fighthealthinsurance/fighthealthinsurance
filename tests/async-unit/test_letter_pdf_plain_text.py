"""The pandoc step that renders letters, health history and cover pages to PDF
reads the text with the markdown extensions that carry markup to the engine
turned off and runs a Lua filter that keeps only an allowlist of plain document
structure, turning every other element into the characters the author typed. So
a backslash command, a code block, math, a raw block or an HTML tag in a letter
or a cover reaches the PDF as the literal characters that were typed rather than
as markup the engine acts on."""

import asyncio
import base64
import json
import os
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fighthealthinsurance.common_view_logic import AppealAssemblyHelper
from fighthealthinsurance.utils import pandoc_convert_command, pandoc_reader_for

from tests.conftest import skip_if_no_pandoc

# Markdown extensions that would otherwise carry markup into the PDF writer.
# Each must be turned off in the reader the text files use.
_DISABLED_TEXT_READER_EXTENSIONS = [
    "raw_tex",
    "raw_attribute",
    "tex_math_dollars",
    "tex_math_single_backslash",
    "tex_math_double_backslash",
    "latex_macros",
    "raw_html",
    "yaml_metadata_block",
]

# The block and inline element types the Lua filter keeps. Everything else is
# turned into the characters it holds, so none of the types below may be left
# in the document the writer receives.
_DISALLOWED_NODE_TYPES = {
    "Code",
    "CodeBlock",
    "Math",
    "RawInline",
    "RawBlock",
    "Image",
    "Link",
    "BlockQuote",
    "Div",
    "Span",
}


def _lua_filter_in(command):
    for arg in command:
        if arg.startswith("--lua-filter="):
            return arg[len("--lua-filter=") :]
    return None


def _run_production_pandoc(input_path, writer, reader=None):
    """Run the command pandoc_convert_command builds for input_path, asking
    pandoc for the given writer on stdout in place of the PDF so the test can
    read what the writer receives. reader, when given, replaces the --from
    value."""
    command = [
        arg
        for arg in pandoc_convert_command(input_path)
        if arg != f"-o{input_path}.pdf"
    ]
    if reader is not None:
        command = [
            f"--from={reader}" if arg.startswith("--from=") else arg for arg in command
        ]
    result = subprocess.run(
        command + ["-t", writer],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _render_to_latex(input_path, reader=None):
    return _run_production_pandoc(input_path, "latex", reader=reader)


def _node_types(parsed):
    """Collect every element-type tag in a parsed pandoc JSON document."""
    tags = set()

    def walk(obj):
        if isinstance(obj, dict):
            tag = obj.get("t")
            if isinstance(tag, str):
                tags.add(tag)
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    walk(parsed)
    return tags


def _parsed_after_render(input_path, reader=None):
    return json.loads(_run_production_pandoc(input_path, "json", reader=reader))


def _node_types_after_render(input_path, reader=None):
    return _node_types(_parsed_after_render(input_path, reader=reader))


def _is_attr(obj):
    """A pandoc JSON Attr is [identifier, [classes], [[key, value], ...]]."""
    return (
        isinstance(obj, list)
        and len(obj) == 3
        and isinstance(obj[0], str)
        and isinstance(obj[1], list)
        and all(isinstance(c, str) for c in obj[1])
        and isinstance(obj[2], list)
        and all(
            isinstance(kv, list)
            and len(kv) == 2
            and all(isinstance(x, str) for x in kv)
            for kv in obj[2]
        )
    )


def _attributes(parsed):
    """Collect every non-empty Attr in a parsed pandoc JSON document."""
    found = []

    def walk(obj):
        if _is_attr(obj):
            if obj != ["", [], []]:
                found.append(obj)
            return
        if isinstance(obj, dict):
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    walk(parsed)
    return found


def _str_values(parsed):
    """Collect the text of every Str element in a parsed pandoc JSON document."""
    values = []

    def walk(obj):
        if isinstance(obj, dict):
            if obj.get("t") == "Str":
                values.append(obj.get("c"))
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    walk(parsed)
    return values


def _render_text_file(text, suffix=".txt", prefix="appealtxt", render=None):
    """Write text to a temporary file and run render (the production command
    with a JSON writer, parsed, by default) on it."""
    render = render or _parsed_after_render
    with tempfile.NamedTemporaryFile(
        suffix=suffix, prefix=prefix, mode="w+t", delete=True
    ) as t:
        t.write(text)
        t.flush()
        return render(t.name)


# A paragraph in LaTeX output that opens with a line break (\\), which every
# LaTeX engine rejects with "There's no line here to end".
_PARAGRAPH_OPENING_WITH_A_LINE_BREAK = re.compile(r"(?:\A|\n[ \t]*\n)[ \t]*\\\\")


class PandocReaderArgumentsTest(unittest.TestCase):
    def test_text_reader_turns_off_the_markup_extensions(self):
        reader = pandoc_reader_for("/tmp/appealtxt.txt")
        self.assertTrue(reader.startswith("markdown"))
        for ext in _DISABLED_TEXT_READER_EXTENSIONS:
            self.assertIn(f"-{ext}", reader)

    def test_iconv_fallback_file_uses_the_same_reader(self):
        # The encoding-repair fallback writes <path>.magic.u8.txt; it must be
        # read the same way as the original text file.
        self.assertEqual(
            pandoc_reader_for("/tmp/appealtxt.txt.magic.u8.txt"),
            pandoc_reader_for("/tmp/appealtxt.txt"),
        )

    def test_text_reader_leaves_smart_typography_on(self):
        # smart only turns straight quotes, dashes and ellipses into their
        # typographic characters, so it stays on.
        self.assertNotIn("-smart", pandoc_reader_for("/tmp/appealtxt.txt"))

    def test_html_input_uses_the_html_reader_with_raw_html_kept(self):
        # The cover letter's .html file is read with pandoc's html reader with
        # raw_html on, so tags it does not convert stay raw HTML (which the
        # filter drops) instead of being loaded while the cover is read.
        reader = pandoc_reader_for("/tmp/info_cover.html")
        self.assertTrue(reader.startswith("html"))
        self.assertIn("+raw_html", reader)

    def test_convert_command_names_the_reader(self):
        command = pandoc_convert_command("/tmp/appealtxt.txt")
        self.assertIn(
            f"--from={pandoc_reader_for('/tmp/appealtxt.txt')}",
            command,
        )

    def test_convert_command_runs_the_plain_text_filter(self):
        filter_path = _lua_filter_in(pandoc_convert_command("/tmp/info_cover.html"))
        self.assertIsNotNone(filter_path)
        self.assertTrue(os.path.isfile(filter_path))

    def test_convert_command_uses_only_options_the_ray_pandoc_accepts(self):
        # The Ray image ships pandoc 2.9, which exits with "Unknown option
        # --sandbox." (the option arrived in 2.15).
        self.assertNotIn("--sandbox", pandoc_convert_command("/tmp/appealtxt.txt"))

    def test_assemble_step_passes_the_reader_to_pandoc(self):
        # _convert_input is the appeal-assembly entry point; confirm the
        # command it hands to pandoc names the hardened reader and the filter.
        helper = AppealAssemblyHelper()
        captured = {}

        async def _fake_engines(command):
            captured["command"] = command

        with tempfile.NamedTemporaryFile(
            suffix=".txt", prefix="appealtxt", mode="w+t", delete=True
        ) as t:
            t.write("Dear reviewer")
            t.flush()
            with patch(
                "fighthealthinsurance.common_view_logic._try_pandoc_engines",
                _fake_engines,
            ):
                asyncio.run(helper._convert_input(t.name))
            command = captured["command"]
            self.assertIn(f"--from={pandoc_reader_for(t.name)}", command)
            self.assertIsNotNone(_lua_filter_in(command))
            joined = " ".join(command)
            for ext in _DISABLED_TEXT_READER_EXTENSIONS:
                self.assertIn(f"-{ext}", joined)


class CustomCoverTemplateTest(unittest.TestCase):
    def test_custom_cover_template_html_escapes_the_substituted_values(self):
        # A domain's cover_template_string is filled with string.Template;
        # the values it receives come out HTML-escaped, as they do in the
        # Django-rendered default cover.
        helper = AppealAssemblyHelper()
        captured = {}

        async def _fake_assemble(input_paths, extra, user_header, target):
            captured["input_paths"] = input_paths
            return target

        with patch.object(helper, "assemble_single_output", _fake_assemble):
            helper._assemble_appeal_pdf(
                insurance_company="Acme <Health>",
                fax_phone="5555555555",
                completed_appeal_text="Dear reviewer",
                company_name="Fight Paperwork",
                patient_name="Jane <b>Doe</b>",
                claim_id="A&B",
                cover_template_string=(
                    "<p>$patient_name for $receiver_name, claim $claim_id</p>"
                ),
                target="/tmp/unused-target.pdf",
            )
        input_paths = captured["input_paths"]
        try:
            cover_path = next(p for p in input_paths if p.endswith(".html"))
            with open(cover_path) as f:
                cover = f.read()
        finally:
            for path in input_paths:
                os.unlink(path)
        self.assertEqual(
            cover,
            "<p>Jane &lt;b&gt;Doe&lt;/b&gt; for Acme &lt;Health&gt;, claim A&amp;B</p>",
        )


# A letter that reaches for every element type the filter must turn into text:
# a heading with attributes, code blocks (including one that ends a verbatim
# environment), inline and display math, a raw LaTeX attribute block, an
# image, a link, HTML tags that would point an engine at a file, a deeply
# nested quote, and a table, a definition list and a footnote holding markup.
_LETTER_WITH_MARKUP = (
    '# Heading {#h .c dir="rtl" style="background-image:url(a.png)"}\n\n'
    "Dear Reviewer,\n\n"
    "Please reconsider my claim. \\input{a.tex} and \\newcommand{\\z}{Z}\\z\n\n"
    "Inline math $x^2 \\input{a.tex}$ and display $$\\input{a.tex}$$\n\n"
    "```{=latex}\n\\input{a.tex}\n```\n\n"
    "    \\end{verbatim}\n    \\input{a.tex}\n\n"
    "~~~\n\\end{verbatim}\n\\input{a.tex}\n~~~\n\n"
    "![a picture](pic.png)\n\n"
    "[a link](a.tex)\n\n"
    '<iframe src="a.tex"></iframe>\n'
    '<object data="a.tex"></object>\n\n'
    "> > > > > > > > > > deep quote \\input{a.tex}\n\n"
    "| a | `\\input{a.tex}` |\n|---|---|\n| $\\input{a.tex}$ | ![p](pic.png) |\n\n"
    "Term `\\input{a.tex}`\n:   $\\input{a.tex}$\n\n"
    "A note.[^n]\n\n"
    "[^n]: ~~~\n    \\end{verbatim}\n    \\input{a.tex}\n    ~~~\n\n"
    "The billed amount was $1,200. Email me at a@b.com, see https://x.example.\n"
)

# A cover built from the kinds of HTML a custom cover template can hold.
_COVER_WITH_MARKUP = (
    "<html><body>"
    '<h1 dir="rtl" style="background-image:url(a.png)">Cover</h1>'
    "<p>Cover for Jane Doe</p>"
    '<table style="background-image:url(a.png)">'
    '<tr style="background-image:url(a.png)">'
    '<td dir="rtl" style="background-image:url(a.png)">cell</td>'
    "</tr></table>"
    "<pre>\\end{verbatim}\n\\input{a.tex}</pre>"
    '<script type="math/tex">\\input{a.tex}</script>'
    "<div>Nested <span>span text</span></div>"
    "</body></html>"
)

# A letter with a pipe table, a definition list and a footnote.
_LETTER_WITH_TABLE_AND_FOOTNOTE = (
    "Dear Team,\n\n"
    "| Date | Service | Amount |\n"
    "|------|---------|--------|\n"
    "| 01/02/2026 | MRI | $1,200 |\n"
    "| 01/09/2026 | Follow-up | $150 |\n\n"
    "Term\n:   Definition\n\n"
    "The policy says so.[^1]\n\n"
    "[^1]: The footnote body.\n"
)


class PandocAllowlistTest(unittest.TestCase):
    """The production command leaves only the allowlist of plain document
    structure, with no attributes, in the document the writer receives. This
    runs the real argv with a JSON writer, so it reports the same result on
    every pandoc version (a LaTeX writer highlights code blocks on newer
    pandoc, which would hide a code block left in place)."""

    @skip_if_no_pandoc
    def test_letter_with_markup_leaves_only_allowlisted_nodes(self):
        with tempfile.NamedTemporaryFile(
            suffix=".txt", prefix="appealtxt", mode="w+t", delete=True
        ) as t:
            t.write(_LETTER_WITH_MARKUP)
            t.flush()
            tags = _node_types_after_render(t.name)
        self.assertEqual(tags & _DISALLOWED_NODE_TYPES, set())

    @skip_if_no_pandoc
    def test_markup_through_the_full_markdown_reader_is_still_text(self):
        # Even a reader that keeps every markup extension on leaves no disallowed
        # node, because the filter is what removes them.
        with tempfile.NamedTemporaryFile(
            suffix=".txt", prefix="appealtxt", mode="w+t", delete=True
        ) as t:
            t.write(_LETTER_WITH_MARKUP)
            t.flush()
            tags = _node_types_after_render(t.name, reader="markdown")
        self.assertEqual(tags & _DISALLOWED_NODE_TYPES, set())

    @skip_if_no_pandoc
    def test_cover_with_markup_leaves_only_allowlisted_nodes(self):
        with tempfile.NamedTemporaryFile(
            suffix=".html", prefix="info_cover", mode="w+t", delete=True
        ) as t:
            t.write(_COVER_WITH_MARKUP)
            t.flush()
            tags = _node_types_after_render(t.name)
        self.assertEqual(tags & _DISALLOWED_NODE_TYPES, set())

    @skip_if_no_pandoc
    def test_letter_elements_carry_no_attributes(self):
        # A heading's identifier, classes and attributes (dir, style) are
        # dropped, so neither a LaTeX nor an HTML engine receives them.
        parsed = _render_text_file(_LETTER_WITH_MARKUP)
        self.assertEqual(_attributes(parsed), [])

    @skip_if_no_pandoc
    def test_cover_elements_carry_no_attributes(self):
        # Headings, tables, rows and cells in a cover keep their text but
        # none of their attributes.
        parsed = _render_text_file(
            _COVER_WITH_MARKUP, suffix=".html", prefix="info_cover"
        )
        self.assertEqual(_attributes(parsed), [])

    @skip_if_no_pandoc
    def test_ordinary_letter_keeps_its_structure(self):
        # The allowlist keeps the structure an ordinary letter uses, so the
        # filter does not flatten a normal appeal.
        letter = (
            "Dear Dr. Smith,\n\n"
            'Please reconsider. The plan covers "medically necessary" care.\n\n'
            "The billed amount was $1,200.\n\n"
            "I am appealing because:\n\n"
            "- it was **medically necessary**\n"
            "- it was *recommended* by my doctor\n\n"
            "Sincerely,\nJane Doe\n"
        )
        with tempfile.NamedTemporaryFile(
            suffix=".txt", prefix="appealtxt", mode="w+t", delete=True
        ) as t:
            t.write(letter)
            t.flush()
            tags = _node_types_after_render(t.name)
        self.assertEqual(tags & _DISALLOWED_NODE_TYPES, set())
        for expected in ("Para", "BulletList", "Strong", "Emph", "Quoted"):
            self.assertIn(expected, tags)

    @skip_if_no_pandoc
    def test_tables_definition_lists_and_footnotes_keep_their_structure(self):
        tags = _node_types(_render_text_file(_LETTER_WITH_TABLE_AND_FOOTNOTE))
        for expected in ("Table", "DefinitionList", "Note"):
            self.assertIn(expected, tags)

    @skip_if_no_pandoc
    def test_table_cells_keep_their_text_separate(self):
        values = _str_values(_render_text_file(_LETTER_WITH_TABLE_AND_FOOTNOTE))
        for cell in ("01/02/2026", "MRI", "$1,200", "01/09/2026", "$150"):
            self.assertIn(cell, values)

    @skip_if_no_pandoc
    def test_footnote_text_is_kept(self):
        values = _str_values(_render_text_file(_LETTER_WITH_TABLE_AND_FOOTNOTE))
        self.assertIn("footnote", values)

    @skip_if_no_pandoc
    def test_blank_code_block_line_is_kept_as_a_visible_space(self):
        # A blank or space-only line in a code block becomes a non-breaking
        # space rather than an empty string, so no line of the block is empty.
        letter = "```\n\nDear Team,\n   \nI am appealing.\n```\n"
        values = _str_values(_render_text_file(letter))
        self.assertNotIn("", values)
        self.assertIn("Dear Team,", values)


class PandocRendersLettersAsTextTest(unittest.TestCase):
    @skip_if_no_pandoc
    def test_backslash_command_in_a_letter_renders_as_literal_text(self):
        letter = (
            "Dear Reviewer,\n\n"
            "Please reconsider my claim. \\input{a.tex}\n\n"
            "The billed amount was $1,200.\n"
        )
        with tempfile.NamedTemporaryFile(
            suffix=".txt", prefix="appealtxt", mode="w+t", delete=True
        ) as t:
            t.write(letter)
            t.flush()
            out = _render_to_latex(t.name)
        # The backslash command is not handed to the engine as a command; it
        # appears as the characters the author typed.
        self.assertNotIn("\\input{a.tex}", out)
        self.assertIn("textbackslash", out)
        # Ordinary letter text and the dollar amount still render.
        self.assertIn("Dear Reviewer", out)
        self.assertIn("1,200", out)

    @skip_if_no_pandoc
    def test_math_markup_in_a_cover_renders_as_literal_text(self):
        # pandoc's html reader turns <script type="math/tex"> into math; the
        # plain-text filter turns that math back into the characters inside.
        cover = (
            "<html><body><p>Cover for Jane Doe "
            '<script type="math/tex">\\input{a.tex}</script>'
            "</p></body></html>"
        )
        with tempfile.NamedTemporaryFile(
            suffix=".html", prefix="info_cover", mode="w+t", delete=True
        ) as t:
            t.write(cover)
            t.flush()
            out = _render_to_latex(t.name)
        self.assertNotIn("\\input{a.tex}", out)
        self.assertIn("textbackslash", out)
        self.assertIn("Cover for Jane Doe", out)

    @skip_if_no_pandoc
    def test_code_block_that_ends_a_verbatim_environment_renders_as_text(self):
        # On older pandoc a code block becomes a verbatim environment; a line
        # that closes it would let the following lines reach the engine. The
        # filter turns the code block into plain lines, so this stays text.
        letter = (
            "Dear Reviewer,\n\n"
            "```\n\\end{verbatim}\n\\input{a.tex}\n```\n\n"
            "Thank you.\n"
        )
        with tempfile.NamedTemporaryFile(
            suffix=".txt", prefix="appealtxt", mode="w+t", delete=True
        ) as t:
            t.write(letter)
            t.flush()
            out = _render_to_latex(t.name)
        self.assertNotIn("\\begin{verbatim}", out)
        self.assertNotIn("\\input{a.tex}", out)
        self.assertIn("Thank you", out)

    @skip_if_no_pandoc
    def test_code_block_starting_with_a_blank_line_renders(self):
        # The LaTeX writer must not open the code block's paragraph with a
        # line break, which every LaTeX engine rejects.
        letter = "```\n\nDear Team,\n\nI am appealing.\n```\n"
        out = _render_text_file(letter, render=_render_to_latex)
        self.assertIsNone(_PARAGRAPH_OPENING_WITH_A_LINE_BREAK.search(out))
        self.assertIn("Dear Team", out)

    @skip_if_no_pandoc
    def test_cover_math_starting_with_a_newline_renders(self):
        cover = (
            '<html><body><p><script type="math/tex">\nx = 1\n</script></p>'
            "</body></html>"
        )
        out = _render_text_file(
            cover, suffix=".html", prefix="info_cover", render=_render_to_latex
        )
        self.assertIsNone(_PARAGRAPH_OPENING_WITH_A_LINE_BREAK.search(out))
        self.assertIn("x = 1", out)

    @skip_if_no_pandoc
    def test_iframe_in_a_cover_is_not_loaded_into_the_document(self):
        # pandoc's html reader loads an iframe's src into the document when
        # raw_html is off; with it on, the iframe stays raw HTML, which the
        # filter drops.
        framed = base64.b64encode(b"<p>Framed page text</p>").decode()
        cover = (
            "<html><body><p>Cover for Jane Doe</p>"
            f'<iframe src="data:text/html;base64,{framed}"></iframe>'
            "</body></html>"
        )
        out = _render_text_file(
            cover, suffix=".html", prefix="info_cover", render=_render_to_latex
        )
        self.assertNotIn("Framed", out)
        self.assertIn("Cover for Jane Doe", out)
