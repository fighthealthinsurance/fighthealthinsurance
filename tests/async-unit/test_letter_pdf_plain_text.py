"""The pandoc step that renders letters, health history and cover pages to PDF
reads the text with its raw-TeX markdown extensions turned off and runs a Lua
filter that turns math and raw TeX into plain text, so a backslash command in a
letter or a cover reaches the PDF as the literal characters the author typed
rather than as markup the LaTeX engine runs."""

import asyncio
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fighthealthinsurance.common_view_logic import AppealAssemblyHelper
from fighthealthinsurance.utils import pandoc_convert_command, pandoc_reader_for

from tests.conftest import skip_if_no_pandoc

# The markdown extensions that would otherwise carry raw TeX into the PDF
# writer. Each must be turned off in the reader the text files use.
_RAW_TEX_EXTENSIONS = [
    "raw_tex",
    "raw_attribute",
    "tex_math_dollars",
    "tex_math_single_backslash",
    "tex_math_double_backslash",
    "latex_macros",
]


def _lua_filter_in(command):
    for arg in command:
        if arg.startswith("--lua-filter="):
            return arg[len("--lua-filter=") :]
    return None


def _render_to_latex(input_path, reader=None):
    """Run the command pandoc_convert_command builds for input_path, asking
    pandoc for LaTeX on stdout in place of the PDF so the test can read what
    would reach the engine. reader, when given, replaces the --from value."""
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
        command + ["-t", "latex"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


class PandocReaderArgumentsTest(unittest.TestCase):
    def test_text_reader_turns_off_the_raw_tex_extensions(self):
        reader = pandoc_reader_for("/tmp/appealtxt.txt")
        self.assertTrue(reader.startswith("markdown"))
        for ext in _RAW_TEX_EXTENSIONS:
            self.assertIn(f"-{ext}", reader)

    def test_iconv_fallback_file_uses_the_same_reader(self):
        # The encoding-repair fallback writes <path>.magic.u8.txt; it must be
        # read the same way as the original text file.
        self.assertEqual(
            pandoc_reader_for("/tmp/appealtxt.txt.magic.u8.txt"),
            pandoc_reader_for("/tmp/appealtxt.txt"),
        )

    def test_html_input_uses_the_html_reader(self):
        # pandoc's html reader does not enable the raw-TeX extensions, so the
        # cover letter's .html file is read with it.
        self.assertEqual(pandoc_reader_for("/tmp/info_cover.html"), "html")

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
            for ext in _RAW_TEX_EXTENSIONS:
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


class PandocRendersLettersAsTextTest(unittest.TestCase):
    @skip_if_no_pandoc
    def test_backslash_command_in_a_letter_renders_as_literal_text(self):
        letter = (
            "Dear Reviewer,\n\n"
            "Please reconsider my claim. \\input{/etc/hostname}\n\n"
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
        self.assertNotIn("\\input{/etc/hostname}", out)
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
            '<script type="math/tex">\\input{/etc/hostname}</script>'
            "</p></body></html>"
        )
        with tempfile.NamedTemporaryFile(
            suffix=".html", prefix="info_cover", mode="w+t", delete=True
        ) as t:
            t.write(cover)
            t.flush()
            out = _render_to_latex(t.name)
        self.assertNotIn("\\input{/etc/hostname}", out)
        self.assertIn("textbackslash", out)
        self.assertIn("Cover for Jane Doe", out)

    @skip_if_no_pandoc
    def test_raw_tex_renders_as_text_with_the_full_markdown_reader(self):
        # The filter turns raw TeX into text even for a reader that keeps the
        # raw-TeX extensions on.
        letter = (
            "Dear Reviewer,\n\n"
            "```{=latex}\n\\input{/etc/hostname}\n```\n\n"
            "Inline $\\input{/etc/hostname}$ too.\n"
        )
        with tempfile.NamedTemporaryFile(
            suffix=".txt", prefix="appealtxt", mode="w+t", delete=True
        ) as t:
            t.write(letter)
            t.flush()
            out = _render_to_latex(t.name, reader="markdown")
        self.assertNotIn("\\input{/etc/hostname}", out)
        self.assertIn("Dear Reviewer", out)
