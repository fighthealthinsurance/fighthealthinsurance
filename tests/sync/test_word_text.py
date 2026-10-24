"""Word files are read in one pass over word/document.xml, within the caps given.

word_text.docx_text keeps paragraphs, tabs and line breaks in their places,
stops at a character cap, a time limit and a cap on unpacked bytes, says
whether it read the whole body, part of it, or nothing, logs nothing and
raises nothing. It has no Django imports, so any worker can use it.
"""

import ast
import io
from pathlib import Path

from loguru import logger

from fighthealthinsurance import word_text
from tests.sync.test_shared_uploads_are_checked_on_the_way_in import zip_bytes

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
ROOM = {"max_chars": 1_000_000, "max_seconds": 60, "max_unpacked_bytes": 10**8}


def word_file(body_xml, prolog=""):
    """A minimal .docx whose word/document.xml holds ``body_xml``."""
    document = (
        f'<?xml version="1.0" encoding="UTF-8"?>{prolog}'
        f'<w:document xmlns:w="{W}"><w:body>{body_xml}</w:body></w:document>'
    )
    return zip_bytes(
        {
            "[Content_Types].xml": b"<Types/>",
            "word/document.xml": document.encode("utf-8"),
        }
    )


def docx_with_a_table():
    import docx

    document = docx.Document()
    document.add_paragraph("Summary Plan Description for the Crumpler Cooperative")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Deductible"
    table.cell(0, 1).text = "$500"
    table.cell(1, 0).merge(table.cell(1, 1)).text = "Out-of-pocket limit"
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


def text_box(text):
    """A run holding a text box the way Word saves one: as a shape, then
    again as a VML picture for readers that do not know shapes."""
    box = f"<w:txbxContent><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:txbxContent>"
    return (
        f'<w:r xmlns:w="{W}">'
        f'<mc:AlternateContent xmlns:mc="{MC}">'
        '<mc:Choice Requires="wps"><w:drawing>'
        '<wp:anchor xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/'
        'wordprocessingDrawing" distT="0" distB="0" distL="0" distR="0" '
        'simplePos="0" relativeHeight="1" behindDoc="0" locked="0" '
        'layoutInCell="1" allowOverlap="1">'
        '<wp:simplePos x="0" y="0"/><wp:extent cx="2743200" cy="914400"/>'
        '<wp:docPr id="1" name="Text Box 1"/>'
        '<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        '<a:graphicData uri="http://schemas.microsoft.com/office/word/2010/'
        'wordprocessingShape">'
        '<wps:wsp xmlns:wps="http://schemas.microsoft.com/office/word/2010/'
        'wordprocessingShape"><wps:cNvSpPr txBox="1"/><wps:spPr/>'
        f"<wps:txbx>{box}</wps:txbx><wps:bodyPr/></wps:wsp>"
        "</a:graphicData></a:graphic></wp:anchor></w:drawing></mc:Choice>"
        '<mc:Fallback><w:pict><v:shape xmlns:v="urn:schemas-microsoft-com:vml" '
        'id="Text Box 1" type="#_x0000_t202" style="width:216pt;height:72pt">'
        f"<v:textbox>{box}</v:textbox></v:shape></w:pict></mc:Fallback>"
        "</mc:AlternateContent></w:r>"
    )


def docx_with_a_text_box(text):
    import docx
    from docx.oxml.parser import parse_xml

    document = docx.Document()
    document.add_paragraph("Summary of Benefits")
    document.add_paragraph()._p.append(parse_xml(text_box(text)))
    document.add_paragraph("Exclusions")
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


def paragraph(text):
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def read(content, **caps):
    return word_text.docx_text(content, **{**ROOM, **caps})


def many_paragraphs(count):
    return "".join(paragraph(f"Crumpler line {n}") for n in range(count))


def test_a_table_reads_cell_by_cell_in_order():
    text, status = read(docx_with_a_table())
    assert "Deductible\n$500\nOut-of-pocket limit" in text
    assert status == word_text.WHOLE


def test_tabs_and_line_breaks_keep_their_places():
    body = (
        "<w:p><w:r><w:t>Deductible</w:t><w:tab/><w:t>$500</w:t><w:br/>"
        "<w:t>Copay</w:t></w:r></w:p>"
    )
    assert read(word_file(body)) == ("Deductible\t$500\nCopay", word_text.WHOLE)


def test_non_breaking_hyphens_and_position_tabs_read_as_text():
    body = (
        "<w:p><w:r><w:t>out</w:t><w:noBreakHyphen/><w:t>of</w:t>"
        "<w:noBreakHyphen/><w:t>network</w:t></w:r></w:p>"
        '<w:p><w:r><w:t>Copay</w:t><w:ptab w:alignment="left" '
        'w:relativeTo="margin" w:leader="none"/><w:t>$50</w:t></w:r></w:p>'
    )
    assert read(word_file(body)) == ("out-of-network\nCopay\t$50", word_text.WHOLE)


def test_a_text_box_saved_in_both_forms_reads_once():
    content = docx_with_a_text_box("Call the Crumpler Cooperative first")
    assert read(content) == (
        "Summary of Benefits\nCall the Crumpler Cooperative first\n\nExclusions",
        word_text.WHOLE,
    )


def test_only_the_first_choice_is_read():
    def alternatives(first, second, fallback):
        return (
            f'<mc:AlternateContent xmlns:mc="{MC}">'
            f'<mc:Choice Requires="wps">{first}</mc:Choice>'
            f'<mc:Choice Requires="w14">{second}</mc:Choice>'
            f"<mc:Fallback>{fallback}</mc:Fallback></mc:AlternateContent>"
        )

    nested = alternatives(paragraph("Copay"), paragraph("again"), paragraph("again"))
    body = (
        paragraph("Deductible")
        + alternatives(nested, paragraph("again"), paragraph("again"))
        + paragraph("Exclusions")
    )
    assert read(word_file(body)) == ("Deductible\nCopay\nExclusions", word_text.WHOLE)


def test_it_stops_reading_at_the_character_cap():
    # More than one chunk, so the cap stops it before the end.
    content = word_file(many_paragraphs(40_000))
    text, status = read(content, max_chars=10)
    assert status == word_text.WHOLE
    assert "Crumpler line 0\n" in text
    assert "Crumpler line 39999" not in text


def test_it_stops_at_the_unpacked_size_cap():
    assert read(word_file(paragraph("Crumpler")), max_unpacked_bytes=10) == (
        "",
        word_text.PARTIAL,
    )


def test_it_stops_at_the_time_limit():
    assert read(word_file(paragraph("Crumpler")), max_seconds=-1) == (
        "",
        word_text.PARTIAL,
    )


def test_a_file_that_is_not_a_word_file_is_unreadable():
    assert read(b"Rebecca Lee Crumpler, not a zip") == ("", word_text.UNREADABLE)


def test_a_zip_without_a_body_is_unreadable():
    assert read(zip_bytes({"readme.txt": b"Crumpler"})) == ("", word_text.UNREADABLE)


def test_a_body_that_breaks_off_keeps_what_was_read():
    # The first chunk reads; the broken markup is past it.
    body = many_paragraphs(40_000) + "<w:p><<<"
    text, status = read(word_file(body))
    assert status == word_text.PARTIAL
    assert text.startswith("Crumpler line 0\nCrumpler line 1\n")


def test_a_body_that_ends_before_it_closes_is_partial():
    # Every read succeeds; only the end of the body shows it is cut short.
    content = zip_bytes(
        {
            "word/document.xml": (
                f'<w:document xmlns:w="{W}"><w:body>'
                f"{paragraph('Rebecca Lee Crumpler Cooperative')}"
                "<w:p><w:r><w:t>Exclusions: cosmetic"
            ).encode("utf-8")
        }
    )
    assert read(content) == ("Rebecca Lee Crumpler Cooperative", word_text.PARTIAL)


def test_entities_are_left_unexpanded():
    prolog = '<!DOCTYPE w:document [<!ENTITY who "rebecca.crumpler@test-fhi.com">]>'
    text, _ = read(word_file(paragraph("Member: &who;"), prolog=prolog))
    assert "rebecca.crumpler" not in text


def test_reading_a_broken_file_logs_nothing_and_raises_nothing():
    logged = []
    sink = logger.add(lambda message: logged.append(str(message)), level="DEBUG")
    try:
        for content in (
            b"rebecca.crumpler@test-fhi.com",
            word_file(paragraph("rebecca.crumpler@test-fhi.com") + "<<<"),
        ):
            read(content)
    finally:
        logger.remove(sink)
    assert logged == []


def test_it_has_no_django_or_app_imports():
    source = Path(word_text.__file__).read_text()
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not {
        name
        for name in imported
        if name.split(".")[0] in ("django", "fighthealthinsurance")
    }
