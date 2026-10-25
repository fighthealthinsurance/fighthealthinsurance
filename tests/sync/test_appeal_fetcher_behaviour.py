"""What the appeals page shows when writing the letters fails, run rather than read.

A failed run used to end on "Try refreshing the page" in the floating progress
box. The page now says what happened in its own panel above the drafts and
offers a button that runs generation again for the same case, in place,
keeping any drafts that already arrived.

Same harness as ``test_entity_fetcher_behaviour``: compile the real
``appeal_fetcher.ts`` with the repo's own ``tsc``, load it in node over
``tests/js/fake_page.cjs`` with the page's jQuery calls stood in for, drive a
run through fake sockets and a fake ``fetch``, and assert on the resulting
page. Every request is recorded and none leaves the process.

Skipped, not silently passed, where node or the front-end toolchain is not
installed. CI installs both.
"""

import json
import pathlib
import subprocess

import pytest
from django.template.loader import render_to_string

from tests.sync.test_entity_fetcher_behaviour import (
    NODE,
    SHIP_LIB,
    SHIP_TARGET,
    TSC,
    needs_node,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
JS = REPO_ROOT / "fighthealthinsurance" / "static" / "js"
FETCHER = JS / "appeal_fetcher.ts"
DRIVER = REPO_ROOT / "tests" / "js" / "appeal_fetcher_behaviour.cjs"
FAKE_PAGE = REPO_ROOT / "tests" / "js" / "fake_page.cjs"

COULD_NOT_WRITE = (
    "We could not finish writing your appeal letters. You can have us try again."
)
COULD_NOT_FINISH = (
    "We could not finish writing all of your appeal letters. The ones below are "
    "ready to use, and trying again keeps them."
)
RETRY = "Try writing the letters again"
RETRYING = "Writing your letters again..."
READY = "Your appeals are ready!"
LETTER_A = "Dear reviewer, the MRI was ordered by Dr. Rebecca Lee Crumpler."
LETTER_B = "Dear reviewer, the plan covers this imaging."
LETTER_C = "Dear reviewer, the guidelines support this imaging."
EDITED = "Dear reviewer, I added my own paragraph to this one."
OPENING = {
    "denial_id": "7",
    "email": "rebecca.crumpler@example.test",
    "semi_sekret": "sekret",
    "csrfmiddlewaretoken": "token",
    "reconnect": False,
}


@pytest.fixture(scope="module")
def compiled(tmp_path_factory) -> pathlib.Path:
    """The real ``appeal_fetcher.ts``, compiled the way the bundle is built."""
    out = tmp_path_factory.mktemp("appeal-fetcher")
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
    built = out / "appeal_fetcher.js"
    if not built.exists():
        pytest.fail(
            f"tsc did not emit appeal_fetcher.js\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    assert result.returncode == 0, result.stdout + result.stderr
    return built


def run_scenario(compiled: pathlib.Path, name: str) -> dict:
    result = subprocess.run(
        [NODE, str(DRIVER), str(compiled), name],
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


def texts(snapshot: dict) -> list[str]:
    return [d["text"] for d in snapshot["drafts"]]


def test_the_fake_page_still_carries_the_real_pages_ids():
    """The fixture is only worth anything while it matches the real page."""
    html = render_to_string(
        "appeals.html",
        {"form_context": {"denial_id": 7}, "denial_id": 7, "use_external": False},
    )
    fixture = FAKE_PAGE.read_text()
    for ident in (
        "loading-text",
        "loading-spinner",
        "loading-more",
        "base-form",
        "output-container",
        "external-models-prompt",
        "request-external-models-btn",
        "external-models-status",
    ):
        assert f'id="{ident}"' in html, ident
        assert f'id="{ident}"' in fixture, ident
    for field in ("proposed_appeal_id", "draft_unsaved", "editted", "presented_ids"):
        assert f'class="{field}"' in html, field
        assert f'class="{field}"' in fixture, field


@needs_node
def test_a_run_that_writes_three_letters_offers_no_retry(compiled):
    ended = run_scenario(compiled, "good_run")["ended"]
    assert texts(ended) == [LETTER_A, LETTER_B, LETTER_C]
    assert ended["panelMessage"] is None
    assert ended["buttons"] == []
    assert ended["statusMessage"].startswith(READY)


@needs_node
def test_a_failed_run_says_so_and_offers_a_retry_instead_of_a_refresh(compiled):
    """The usual production failure: the socket goes quiet, the backup fails."""
    ended = run_scenario(compiled, "rest_fallback_fails")["ended"]
    assert ended["panelMessage"] == COULD_NOT_WRITE
    assert ended["buttons"] == [{"text": RETRY, "type": "button"}]
    assert ended["panelClass"] == "fhi-notice fhi-notice-warning fhi-stack"
    assert "refresh" not in ended["visibleText"].lower(), ended["visibleText"]
    # Nothing still claims to be generating over the answer.
    assert ended["indicatorShown"] is False
    assert ended["loadingTextDisplay"] == "none"
    assert ended["movedThePerson"] == []
    assert ended["errorReports"] == 1


@needs_node
def test_the_panel_is_a_live_region_above_the_drafts(compiled):
    ended = run_scenario(compiled, "rest_fallback_fails")["ended"]
    assert ended["panelRole"] == "status"
    assert ended["panelLive"] == "polite"
    assert ended["panelSitsAboveTheDrafts"] is True


@needs_node
def test_a_run_that_ends_with_nothing_is_not_called_ready(compiled):
    """Every socket closed cleanly and no letter arrived.

    This used to paint "Your appeals are ready!" and leave an empty page.
    """
    ended = run_scenario(compiled, "nothing_arrives")["ended"]
    assert ended["drafts"] == []
    assert ended["panelMessage"] == COULD_NOT_WRITE
    assert [b["text"] for b in ended["buttons"]] == [RETRY]
    assert READY not in ended["visibleText"]


@needs_node
def test_a_failure_the_server_reported_offers_a_retry(compiled):
    """The server's error frame counts, even with a letter on the page."""
    result = run_scenario(compiled, "server_reports_the_failure")
    ended = result["ended"]
    assert texts(ended) == [LETTER_A]
    assert ended["panelMessage"] == COULD_NOT_FINISH
    assert [b["text"] for b in ended["buttons"]] == [RETRY]


@needs_node
def test_a_retry_that_only_replays_the_kept_drafts_is_still_a_failure(compiled):
    """The two letters served again are the two already on the page."""
    ended = run_scenario(
        compiled, "retry_that_only_replays_the_kept_drafts_then_fails"
    )["ended"]
    assert texts(ended) == [LETTER_A, LETTER_B]
    assert ended["panelMessage"] == COULD_NOT_FINISH
    assert [b["text"] for b in ended["buttons"]] == [RETRY]
    assert READY not in ended["visibleText"]


@needs_node
def test_the_page_does_not_say_ready_between_automatic_attempts(compiled):
    between = run_scenario(compiled, "server_reports_the_failure")["betweenAttempts"]
    assert between["indicatorShown"] is True
    assert READY not in between["statusMessage"], between["statusMessage"]


@needs_node
def test_the_retry_runs_again_in_place_for_the_same_case(compiled):
    result = run_scenario(compiled, "retry_runs_again_in_place")
    assert result["ended"]["socketCount"] == 1
    assert result["opening"] == OPENING
    during = result["during"]
    assert during["socketCount"] == 2
    # The button is gone while the run it started is going.
    assert during["buttons"] == []
    assert during["panelMessage"] is None
    assert during["indicatorShown"] is True
    assert during["statusMessage"] == RETRYING
    assert during["loadingTextDisplay"] == "block"
    assert during["movedThePerson"] == []
    finished = result["finished"]
    assert texts(finished) == [LETTER_A, LETTER_B, LETTER_C]
    assert finished["panelMessage"] is None
    assert finished["statusMessage"].startswith(READY)
    assert finished["movedThePerson"] == []


@needs_node
def test_the_retry_keeps_the_drafts_that_arrived_and_the_edits_in_them(compiled):
    """The server serves a letter it already has again; it is not added twice."""
    result = run_scenario(compiled, "retry_keeps_the_drafts_that_arrived")
    ended = result["ended"]
    assert texts(ended) == [LETTER_A]
    assert ended["panelMessage"] == COULD_NOT_FINISH
    during = result["during"]
    assert texts(during) == [EDITED]
    finished = result["finished"]
    assert finished["drafts"] == [
        {"id": "magic1", "text": EDITED},
        {"id": "magic2", "text": LETTER_B},
        {"id": "magic3", "text": LETTER_C},
    ]
    assert finished["panelMessage"] is None


@needs_node
def test_a_second_press_starts_nothing(compiled):
    result = run_scenario(compiled, "second_press_starts_nothing")
    # Five sockets for the failed run, one for the retry.
    assert result["afterTwoPresses"]["socketCount"] == 6


@needs_node
def test_the_external_models_button_cannot_start_a_run_beside_a_retry(compiled):
    result = run_scenario(compiled, "second_press_starts_nothing")
    after = result["afterTheOtherButton"]
    assert after["externalPromptDisplay"] == "none"
    assert after["enableRequests"] == 0
    assert after["socketCount"] == 6


@needs_node
def test_a_retry_pressed_while_the_external_models_request_is_out_waits(compiled):
    result = run_scenario(compiled, "retry_waits_for_the_external_models_request")
    assert result["whileTheRequestIsOut"]["socketCount"] == 5
    after = result["afterItCameBack"]
    assert after["socketCount"] == 6
    assert after["buttons"] == []
    assert after["externalPromptDisplay"] == "none"


@needs_node
def test_the_failed_sockets_own_close_changes_nothing(compiled):
    """The browser fires close after error, on the socket that ended the run."""
    result = run_scenario(compiled, "the_failed_sockets_own_close_changes_nothing")
    assert result["afterItsOwnClose"] == result["ended"]


@needs_node
def test_the_last_runs_close_event_cannot_end_the_retry(compiled):
    """close() only asks the browser to hang up.

    The close event of the socket the page gave up on lands a network round
    trip later, and the retry button is on the screen for all of it. Read as
    this run's ending, it would start an automatic attempt beside the one the
    person asked for.
    """
    result = run_scenario(compiled, "stale_close_lands_during_the_retry")
    assert result["ended"]["panelMessage"] == COULD_NOT_WRITE
    during = result["afterTheOldClose"]
    assert during["socketCount"] == 2, during
    assert during["buttons"] == []
    assert during["indicatorShown"] is True
    finished = result["finished"]
    assert texts(finished) == [LETTER_A, LETTER_B, LETTER_C]
    assert finished["socketCount"] == 2


@needs_node
def test_the_last_runs_tidy_up_does_not_hide_the_retry(compiled):
    """The failed run booked the loading text's removal a second out."""
    during = run_scenario(compiled, "stale_close_lands_during_the_retry")[
        "afterTheOldClose"
    ]
    assert during["loadingTextDisplay"] == "block"


@needs_node
def test_a_retry_never_shows_the_advice_to_refresh(compiled):
    """The first wait's explainer says to refresh, which would lose any edits.

    Pressed inside the second the failed run waits before hiding it, the retry
    cancels that hide, so it has to take the explainer down itself.
    """
    during = run_scenario(compiled, "stale_close_lands_during_the_retry")[
        "afterTheOldClose"
    ]
    assert "refresh" not in during["visibleText"].lower(), during["visibleText"]


@needs_node
def test_a_frame_from_the_last_run_cannot_reach_the_retry(compiled):
    """Half a frame from the old socket would eat the retry's first letter."""
    finished = run_scenario(compiled, "stale_frame_lands_during_the_retry")["finished"]
    assert texts(finished) == [LETTER_A, LETTER_B, LETTER_C]
    assert finished["socketCount"] == 2


@needs_node
def test_an_error_from_the_last_run_cannot_reach_the_retry(compiled):
    """Read as this run's, it would tell the server the retry is a reconnect."""
    result = run_scenario(compiled, "stale_error_lands_during_the_retry")
    assert result["opening"] == OPENING
    assert result["during"]["statusMessage"] == RETRYING


@needs_node
def test_a_reconnect_booked_by_the_last_run_never_opens(compiled):
    result = run_scenario(compiled, "reconnect_booked_by_the_failed_run_never_opens")
    assert result["ended"]["panelMessage"] == COULD_NOT_WRITE
    after = result["afterTheOldReconnect"]
    assert after["socketCount"] == 2, after
    assert after["buttons"] == []


@needs_node
def test_an_external_models_rerun_keeps_its_progress_panel(compiled):
    """The last run's success paint booked the panel's removal three seconds out."""
    result = run_scenario(compiled, "external_rerun_keeps_its_progress_panel")
    assert result["ended"]["statusMessage"].startswith(READY)
    assert result["duringTheRerun"]["indicatorShown"] is True
