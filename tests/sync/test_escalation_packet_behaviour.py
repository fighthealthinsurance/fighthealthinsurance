"""What the escalation page actually shows, run rather than read.

The escalation half of ``tests/async-unit/test_entity_extract_frames.py`` pins
the server's frames properly and then reads ``escalation_packet.html`` as text
for the page-side rules. That fallback is weak in a demonstrated way: a
reviewer restored the whole defect this branch removes, as
``loadingText.setAttribute('style', 'display:none')`` at the top of the close
handler, and all three source-text assertions still passed. A grep catches a
rule being deleted. It does not catch the page doing the wrong thing.

So this renders the real template, lifts its script out of the rendered HTML,
and runs it in node over a hand-written page (``tests/js/fake_page.cjs``),
driving a packet through a fake socket and asserting on the resulting DOM.
Rendered rather than read as a file, because what ships is the render: the
script sits behind ``{% if recipients_count > 0 %}`` and carries a Django tag
inside it, and a harness that reads the raw template would be testing
something the browser never sees.

The defect the escalation page had is the same one the extraction page had.
``ws.onclose`` hid the loading block, so a socket that died after two of four
letters looked exactly like a finished packet: the spinner went away, two
letters sat on the screen, and nothing said the other two were never written.
The close handler also ran after ``onerror``, erasing the connection error it
had just put on the screen.

Skipped, not silently passed, where node is not installed. CI installs it: the
sync job sets node up before it runs tox.
"""

import json
import pathlib
import re
import shutil
import subprocess

import pytest
from django.template.loader import render_to_string

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
APP = REPO_ROOT / "fighthealthinsurance"
ESCALATION_TEMPLATE = APP / "templates" / "escalation_packet.html"
DRIVER = REPO_ROOT / "tests" / "js" / "escalation_packet_behaviour.cjs"
FAKE_PAGE = REPO_ROOT / "tests" / "js" / "fake_page.cjs"

NODE = shutil.which("node")

needs_node = pytest.mark.skipif(NODE is None, reason="needs node")

CONTEXT = {
    "recipients_count": 2,
    "recipients": [
        {"name": "State Insurance Commissioner", "phone": "555-0100"},
        {"name": "State AG"},
    ],
    "form_context": {"denial_id": 5, "email": "someone@example.test"},
    "denial_id": 5,
    "user_email": "someone@example.test",
    "semi_sekret": "sekret",
    "back_url": "/back/",
    "back_label": "Back",
}

STILL_DRAFTING = "drafting your regulator letters"


@pytest.fixture(scope="module")
def page_script(tmp_path_factory) -> pathlib.Path:
    """The escalation page's script, as rendered, written out for node."""
    html = render_to_string("escalation_packet.html", CONTEXT)
    blocks = re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL)
    ours = [b for b in blocks if "streaming-escalation-backend" in b]
    assert len(ours) == 1, f"{len(ours)} candidate scripts in the rendered page"
    body = ours[0]
    # An unrendered tag here means the extraction picked up template source
    # instead of output, and node would die on it with a syntax error that says
    # nothing useful.
    assert "{{" not in body and "{%" not in body, body
    out = tmp_path_factory.mktemp("escalation") / "page.js"
    out.write_text(body)
    return out


def run_scenario(page_script: pathlib.Path, name: str) -> dict:
    result = subprocess.run(
        [NODE, str(DRIVER), str(page_script), name],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        pytest.fail(
            f"scenario {name} crashed\nstdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return json.loads(result.stdout)


def test_the_fake_page_still_carries_the_real_pages_ids():
    """The fixture is only worth anything while it matches the real page.

    Every assertion below reaches for an element by the id the template gives
    it. If the template renames one and the fixture keeps the old name, the
    page script would find nothing, take all its ``if (loadingText)`` branches
    the quiet way, and the tests would pass against a page that does nothing.
    """
    html = render_to_string("escalation_packet.html", CONTEXT)
    fixture = FAKE_PAGE.read_text()
    for ident in (
        "loading-text",
        "escalation-letters",
        "base-letter-form",
        "escalation-form-context",
    ):
        assert f'id="{ident}"' in html, ident
        assert f'id="{ident}"' in fixture, ident
    # The clone path reaches into the hidden form by tag and by input name.
    assert 'name="escalation_uuid"' in html
    assert 'name="escalation_uuid"' in fixture
    assert "<textarea" in html
    assert "<textarea" in fixture


@needs_node
def test_a_socket_that_dies_mid_packet_is_not_a_finished_packet(page_script):
    """Two of four letters and a vanished spinner is the lie this removes."""
    result = run_scenario(page_script, "close_without_done")
    before = result["beforeTheClose"]
    assert before["letterCount"] == 2, before
    assert STILL_DRAFTING in before["loadingHeading"].lower(), before

    ended = result["ended"]
    # The letters that did arrive stay.
    assert ended["letterCount"] == 2, ended
    # The block is still on the screen, and it no longer claims we are working.
    assert ended["loadingDisplay"] != "none", ended
    assert ended["loadingHeading"] == "The connection ended early", ended
    assert STILL_DRAFTING not in ended["visibleText"].lower(), ended
    # And it says how much of the packet the person actually has.
    assert "2 letter(s)" in ended["loadingDetail"], ended
    assert "Reload this page" in ended["loadingDetail"], ended
    assert ended["loadingBorderLeft"] == "4px solid #dc3545", ended
    assert ended["movedThePerson"] == [], ended


@needs_node
def test_a_socket_that_says_nothing_at_all_is_not_a_finished_packet(page_script):
    ended = run_scenario(page_script, "close_with_nothing_at_all")["ended"]
    assert ended["letterCount"] == 0, ended
    assert ended["loadingDisplay"] != "none", ended
    assert ended["loadingHeading"] == "The connection ended early", ended
    assert "0 letter(s)" in ended["loadingDetail"], ended


@needs_node
def test_only_a_complete_packet_takes_the_block_off_the_screen(page_script):
    """The one honest reason to hide it, and it has to still work."""
    result = run_scenario(page_script, "done_complete")
    assert result["beforeTheClose"]["loadingDisplay"] == "none", result
    ended = result["ended"]
    assert ended["loadingDisplay"] == "none", ended
    assert ended["letterCount"] == 2, ended
    assert STILL_DRAFTING not in ended["visibleText"].lower(), ended
    # The close after a finished packet is the socket hanging up, not a second
    # answer: nothing may put the connection-ended words up over it.
    assert "connection ended early" not in ended["visibleText"].lower(), ended


@needs_node
def test_a_done_frame_with_letters_missing_says_which_are_missing(page_script):
    """``done`` is not ``complete``. The server skips what it cannot draft."""
    result = run_scenario(page_script, "done_incomplete")
    before = result["beforeTheClose"]
    assert before["loadingDisplay"] != "none", before
    assert before["loadingHeading"] == "Some letters are missing", before
    assert "2 of 4 letters are ready" in before["loadingDetail"], before
    assert "Federal Ombudsman" in before["loadingDetail"], before
    assert "Plan Appeals Board" in before["loadingDetail"], before
    # The close that follows must not tidy that away.
    assert result["ended"] == before, result


@needs_node
def test_the_close_does_not_erase_the_connection_error(page_script):
    """``onerror`` then ``onclose`` is the ordinary order for a dropped socket."""
    result = run_scenario(page_script, "error_then_close")
    after = result["afterTheError"]
    assert after["loadingHeading"] == "Connection error", after
    ended = result["ended"]
    assert ended["loadingDisplay"] != "none", ended
    assert ended["loadingHeading"] == "Connection error", ended
    assert "lost the connection" in ended["loadingDetail"], ended
    assert ended["letterCount"] == 1, ended


@needs_node
def test_a_progress_message_cannot_paint_over_bad_news(page_script):
    """A status frame queued behind an error would have looked like recovery."""
    result = run_scenario(page_script, "status_cannot_paint_over_a_failure")
    after_error = result["afterTheError"]
    assert after_error["loadingHeading"] == "We hit a problem", after_error
    assert "could not reach" in after_error["loadingDetail"], after_error
    after_status = result["afterTheStatus"]
    assert after_status["loadingDetail"] == after_error["loadingDetail"], after_status
    assert "Drafting letter 2 of 4" not in after_status["visibleText"], after_status
