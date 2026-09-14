"""What the extraction page actually shows, run rather than read.

The source-text assertions in ``tests/async-unit/test_entity_extract_frames``
catch a rule being deleted and nothing more: a reviewer restored the deleted
auto-advance as ``form.requestSubmit()`` and every one of them still passed.

So this compiles the real TypeScript with the repo's own ``tsc``, loads it in
node over ``tests/js/fake_page.cjs``, drives a run through a fake socket and
asserts on the resulting DOM. The page object records every ``click()``,
``submit()``, ``requestSubmit()`` and write to ``location`` on the flow's real
form and Next button, so an auto-advance under any spelling shows up as a call
in the log rather than as a missing string in the source.

Same source and the same emit, but not the same artifact: what happens after
tsc, webpack's wrapping and the Terser pass, is not covered here.

Skipped, not silently passed, where node or the front-end toolchain is not
installed: ``node_modules`` is gitignored, so a checkout that has never run
``npm install`` cannot run these. CI is not one of those checkouts.
"""

import json
import os
import pathlib
import re
import shutil
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
APP = REPO_ROOT / "fighthealthinsurance"
JS = APP / "static" / "js"
FETCHER = JS / "entity_fetcher.ts"
TSC = JS / "node_modules" / "typescript" / "bin" / "tsc"
DRIVER = REPO_ROOT / "tests" / "js" / "entity_fetcher_behaviour.cjs"
FAKE_PAGE = REPO_ROOT / "tests" / "js" / "fake_page.cjs"
ENTITY_TEMPLATE = APP / "templates" / "entity_extract.html"
FLOW_TEMPLATE = APP / "templates" / "single_optional_question.html"

NODE = shutil.which("node")

needs_node = pytest.mark.skipif(
    NODE is None or not TSC.exists(),
    reason=(
        "needs node and the front-end toolchain: "
        "npm install in fighthealthinsurance/static/js"
    ),
)

RETRY_BUTTON = "Try reading the letter again"
CONTINUE_GOOD = "Continue to the next page"
CONTINUE_TYPING = "Continue and type it in myself"
COULD_NOT_READ = (
    "We could not finish reading your letter. You can have us try again, or "
    "type the details in yourself."
)


TSCONFIG = JS / "tsconfig.json"

# The settings the shipped bundle is built with, from
# ``static/js/tsconfig.json``. ``module`` is the one knob that has to differ,
# because node has to ``require`` the output.
SHIP_TARGET = "es5"
SHIP_LIB = "dom,dom.iterable,esnext"


@pytest.fixture(scope="module")
def compiled(tmp_path_factory) -> pathlib.Path:
    """The real ``entity_fetcher.ts``, compiled the way the bundle is built.

    Compiled rather than loaded from ``static/js/dist``, where the committed
    bundle can be older than the source. The flags mirror
    ``static/js/tsconfig.json``: at ``es5`` the object spread and every arrow
    function are downlevelled, which is the code production runs, and at
    ES2019 they are not.
    """
    out = tmp_path_factory.mktemp("entity-fetcher")
    result = subprocess.run(
        [
            NODE,
            str(TSC),
            "--target",
            SHIP_TARGET,
            "--module",
            "commonjs",
            "--moduleResolution",
            "node",
            "--lib",
            SHIP_LIB,
            "--strict",
            "--esModuleInterop",
            "--allowSyntheticDefaultImports",
            "--forceConsistentCasingInFileNames",
            "--skipLibCheck",
            "--outDir",
            str(out),
            str(FETCHER),
        ],
        cwd=str(JS),
        capture_output=True,
        text=True,
        timeout=300,
    )
    built = out / "entity_fetcher.js"
    if not built.exists():
        pytest.fail(
            f"tsc did not emit entity_fetcher.js\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    # Emit happens even with type errors, so the exit code is checked
    # separately: the page shipping with a type error is its own problem.
    assert result.returncode == 0, result.stdout + result.stderr
    return built


def run_scenario(compiled: pathlib.Path, name: str) -> dict:
    env = dict(os.environ, NODE_ENV="test")
    result = subprocess.run(
        [NODE, str(DRIVER), str(compiled), name],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    if result.returncode != 0:
        pytest.fail(
            f"scenario {name} crashed\nstdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return json.loads(result.stdout)


def button_texts(snapshot: dict) -> list[str]:
    return [b["text"] for b in snapshot["buttons"]]


def submit_button(snapshot: dict) -> dict:
    submits = [b for b in snapshot["buttons"] if b["type"] == "submit"]
    assert len(submits) == 1, snapshot["buttons"]
    return submits[0]


@needs_node
def test_the_fake_page_still_carries_the_real_pages_ids(compiled):
    """The fixture is only worth anything while it matches the real page.

    An auto-advance grabs the flow's form or its Next button by id. If the
    template renames one and the fixture keeps the old name, the navigation
    assertions below would pass against a page where there was nothing to grab.
    """
    fixture = FAKE_PAGE.read_text()
    assert 'id="waiting-msg"' in ENTITY_TEMPLATE.read_text()
    flow = FLOW_TEMPLATE.read_text()
    assert 'id="fuck_health_insurance_form"' in flow
    assert 'id="next"' in flow
    for ident in ("waiting-msg", "fuck_health_insurance_form", "next"):
        assert f'id="{ident}"' in fixture, ident


def test_the_harness_compiles_the_way_the_bundle_does():
    """The fixture's flags have to keep matching the shipped build.

    Without this the fixture drifts silently: someone raises the bundle's
    target to ES2020, the harness carries on compiling at whatever it was
    pinned to, and the claim that these tests run the shipping code becomes
    false with nothing failing. No node needed, so it runs everywhere.
    """
    config = json.loads(re.sub(r"//[^\n]*", "", TSCONFIG.read_text()))
    options = config["compilerOptions"]
    assert options["target"].lower() == SHIP_TARGET, options["target"]
    assert ",".join(sorted(x.lower() for x in options["lib"])) == ",".join(
        sorted(SHIP_LIB.split(","))
    ), options["lib"]
    assert options["strict"] is True, options
    # The bundle is ES modules and this has to be requirable, so ``module`` is
    # deliberately not matched. Named here so the exception is a decision
    # rather than an oversight.
    assert options["module"] == "es2020", options["module"]


@needs_node
def test_a_good_run_ends_on_the_words_for_a_good_run(compiled):
    result = run_scenario(compiled, "good_run")
    # The page sends what it was handed, and says this is not a retry.
    assert result["opening"] == {"denial_id": 7, "retry": False}
    ended = result["ended"]
    assert ended["title"].startswith("We read your letter and filled in what we found")
    assert button_texts(ended) == [RETRY_BUTTON, CONTINUE_GOOD]
    assert submit_button(ended)["text"] == CONTINUE_GOOD
    assert CONTINUE_TYPING not in ended["visibleText"]
    assert ended["borderColor"] == "#28a745"


@needs_node
def test_the_page_stops_saying_it_is_still_reading_once_the_run_ends(compiled):
    """Two answers on one page is the defect, whichever one is true.

    ``#waiting-msg`` is a spinner and the words "Analyzing your denial...",
    and the auto-advance that used to take it off screen is gone. Asserted on
    what is visible rather than on what is in the tree, because hiding it is
    the fix.
    """
    for scenario in ("good_run", "nothing_found", "dead_socket"):
        result = run_scenario(compiled, scenario)
        ended = result["ended"]
        assert ended["waitingDisplay"] == "none", scenario
        assert "analyzing your denial" not in ended["visibleText"].lower(), scenario
        assert ended["waitingStillSaysAnalyzing"] is True, scenario


@needs_node
def test_nothing_on_this_page_moves_the_person(compiled):
    """The flow's form and its Next button are on the fixture page.

    A restored auto-advance has the same things to grab here as in a browser,
    so this fails on ``.click()``, ``.submit()``, ``.requestSubmit()`` and on a
    write to ``location`` alike, rather than on a spelling.
    """
    for scenario in (
        "good_run",
        "nothing_found",
        "dead_socket",
        "inactivity_timeout",
        "unlabeled_step_never_renders",
        "late_frame_cannot_repaint",
        "retry_button_runs_again",
        "close_event_lands_during_the_next_run",
        "stale_frame_lands_during_the_next_run",
        "reconnect_from_the_previous_run_never_opens",
        "reconnect_after_the_verdict_never_opens",
    ):
        result = run_scenario(compiled, scenario)
        for key, snapshot in result.items():
            if not isinstance(snapshot, dict) or "movedThePerson" not in snapshot:
                continue
            assert snapshot["movedThePerson"] == [], (scenario, key, snapshot)


@needs_node
def test_every_terminal_state_offers_both_ways_out(compiled):
    """A state that offers one and not the other is how people got stranded."""
    expected = {
        "good_run": CONTINUE_GOOD,
        "nothing_found": CONTINUE_TYPING,
        "dead_socket": CONTINUE_TYPING,
        "inactivity_timeout": CONTINUE_TYPING,
    }
    for scenario, continue_text in expected.items():
        ended = run_scenario(compiled, scenario)["ended"]
        assert button_texts(ended) == [RETRY_BUTTON, continue_text], scenario
        assert ended["actionsDisplay"] == "flex", scenario
        assert submit_button(ended)["text"] == continue_text, scenario


@needs_node
def test_a_socket_that_says_nothing_is_not_a_finished_run(compiled):
    """The run the page used to paint green and click Next on."""
    result = run_scenario(compiled, "dead_socket")
    ended = result["ended"]
    assert ended["title"] == COULD_NOT_READ
    assert ended["borderColor"] == "#dc3545"
    # Two reconnects for a blip, and then an answer. Not a loop.
    assert ended["socketCount"] == 3, ended


@needs_node
def test_a_run_that_goes_quiet_lands_on_could_not_read(compiled):
    result = run_scenario(compiled, "inactivity_timeout")
    before = result["beforeTheTimeout"]
    assert before["title"] == "Reading your denial letter"
    assert before["buttons"] == []
    ended = result["ended"]
    assert ended["title"] == COULD_NOT_READ
    assert ended["borderColor"] == "#dc3545"


@needs_node
def test_no_internal_step_name_reaches_the_page(compiled):
    """A frame with no words for the person is not rendered at all.

    The old client fell back to "Processed ${taskName}", which put "triage" and
    "plan document summary" in front of patients.
    """
    ended = run_scenario(compiled, "unlabeled_step_never_renders")["ended"]
    visible = ended["visibleText"]
    assert "plan_document_summary" not in visible, visible
    assert "plan document summary" not in visible.lower(), visible
    assert "extract_set_triage" not in visible, visible
    assert "triage" not in visible.lower(), visible
    # The labelled step next to them did render, so this is the absence of a
    # fallback and not the absence of rendering.
    assert "Fax number: found" in visible, visible


@needs_node
def test_nothing_paints_over_a_terminal_state(compiled):
    """A reconnect can deliver frames after the page has given its answer."""
    result = run_scenario(compiled, "late_frame_cannot_repaint")
    assert result["afterTheLateFrames"] == result["ended"]
    assert "Plan ID" not in result["afterTheLateFrames"]["visibleText"]


@needs_node
def test_the_socket_closing_after_a_finished_run_changes_nothing(compiled):
    """``finish`` hangs up, and the close event lands a moment later.

    Two guards stop that event being read as a second answer: the one in
    ``settleConnection`` and the one at the top of ``finish``. Either alone is
    enough, so this only fails when both are gone. That is what a
    defence-in-depth test is worth, and it is worth saying so.
    """
    result = run_scenario(compiled, "good_run")
    assert result["afterTheSocketClosed"] == result["ended"]


@needs_node
def test_the_last_runs_close_event_cannot_end_this_run(compiled):
    """Found by running the page rather than reading it.

    ``finish`` closes the socket, but ``close()`` only asks the browser to hang
    up: the close event arrives after the closing handshake, a network round
    trip later, and the retry button is on the screen for the whole of that
    window. That event used to reach ``settleConnection``, which had already
    seen ``settled`` go back to False, so pressing retry painted "We could not
    finish reading your letter" over a run that had not finished yet, and the
    answer that run went on to produce was then dropped by the ``settled``
    guard in ``handleFrame``. Reporting a failure that did not happen is the
    same defect as reporting a success that did not happen.
    """
    result = run_scenario(compiled, "close_event_lands_during_the_next_run")
    during = result["afterTheOldClose"]
    assert during["title"] == "Reading your denial letter", during
    assert during["buttons"] == [], during
    assert COULD_NOT_READ not in during["visibleText"], during
    ended = result["ended"]
    assert ended["title"].startswith("We read your letter and filled in what we found")
    assert button_texts(ended) == [RETRY_BUTTON, CONTINUE_GOOD]
    # The stale socket does not get replaced by a reconnect either.
    assert result["socketCount"] == 2, result


@needs_node
def test_a_frame_from_the_last_run_cannot_speak_for_this_one(compiled):
    """The generation guard in ``onmessage``, on its own.

    ``finish`` calls ``close()``, which asks the browser to hang up; frames
    already on the wire are still delivered. ``settled`` is no help here,
    because pressing retry set it back to False on purpose, so the stale
    run-level frame would go straight through ``handleFrame`` into ``finish``
    and paint the old run's verdict over a run still in flight. This is the
    green version of that: "we read your letter and filled in what we found",
    claimed for a read that had not happened yet.
    """
    result = run_scenario(compiled, "stale_frame_lands_during_the_next_run")
    during = result["afterTheStaleFrames"]
    assert during["title"] == "Reading your denial letter", during
    assert during["buttons"] == [], during
    assert "filled in what we found" not in during["visibleText"], during
    # The stale step line does not get appended under the live run either.
    assert "Plan ID" not in during["visibleText"], during
    assert during["steps"] == "Reading your letter again...", during
    ended = result["ended"]
    assert ended["title"].startswith("We read your letter and did not find")
    assert button_texts(ended) == [RETRY_BUTTON, CONTINUE_TYPING]


@needs_node
def test_a_reconnect_booked_by_the_last_run_never_opens(compiled):
    """The generation guard on the reconnect timer, on its own.

    A socket that blips books a reconnect a second out. If the run is over
    before that second is up (the inactivity timer fires, the person presses
    retry), the reconnect is for a run nobody is looking at any more. Without
    the guard it opens a fourth socket and takes the ``activeSocket`` slot from
    the live run, so when the live run finishes the page hangs up on the stale
    socket and leaves the real one open.
    """
    result = run_scenario(compiled, "reconnect_from_the_previous_run_never_opens")
    ended = result["ended"]
    assert ended["title"] == COULD_NOT_READ, ended
    assert ended["socketCount"] == 2, ended
    after = result["afterTheOldReconnect"]
    # Two sockets for the abandoned run, one for the run the person asked for.
    assert after["socketCount"] == 3, after
    assert after["title"] == "Reading your denial letter", after
    assert after["buttons"] == [], after
    finished = result["finished"]
    assert finished["socketCount"] == 3, finished
    assert finished["title"].startswith(
        "We read your letter and filled in what we found"
    )
    # The page hung up on the second socket when the run timed out, and on the
    # live run's own socket when it finished. The first was closed by the
    # server, not by us.
    assert result["hungUpOn"] == [False, True, True], result["hungUpOn"]


@needs_node
def test_a_reconnect_booked_before_the_verdict_never_opens(compiled):
    """No retry press, no generation change, and still a socket too many.

    A socket that blips books a reconnect a second out. If the run's own
    inactivity timer comes due inside that second, the page has already given
    the person its terminal verdict when the reconnect falls due. The
    generation guard does not cover this, because nothing started a new run:
    the reconnect opens a socket and asks the server to read the letter again
    under a verdict that is already on the screen.
    """
    result = run_scenario(compiled, "reconnect_after_the_verdict_never_opens")
    before = result["beforeTheTimeout"]
    assert before["socketCount"] == 1, before
    ended = result["ended"]
    assert ended["title"] == COULD_NOT_READ, ended
    assert ended["socketCount"] == 1, ended
    after = result["afterTheBookedReconnect"]
    assert after["socketCount"] == 1, after
    assert after["title"] == COULD_NOT_READ, after
    assert button_texts(after) == [RETRY_BUTTON, CONTINUE_TYPING], after


@needs_node
def test_the_retry_button_runs_again_in_place_and_says_it_is_a_retry(compiled):
    """The retry is a real operation, and the server has to be told.

    Returning to the extraction URL re-runs nothing: the already-done gate is
    an OR over the finished flag, so without the flag on the payload the button
    would reconnect and be told the same nothing over again.
    """
    result = run_scenario(compiled, "retry_button_runs_again")
    assert result["secondOpening"] == {"denial_id": 7, "retry": True}
    assert result["socketCount"] == 2
    during = result["duringTheSecondRun"]
    assert during["title"] == "Reading your denial letter"
    assert during["steps"] == "Reading your letter again..."
    # The terminal state's controls are gone while the second run is in flight,
    # so there is nothing to press twice.
    assert during["buttons"] == []
    assert during["actionsDisplay"] == "none"
    assert during["movedThePerson"] == []
