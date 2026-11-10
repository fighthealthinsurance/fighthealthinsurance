"""The request-side eligibility detector (chat/eligibility_intent.py).

It decides when a chat turn tells the model to run the Medicaid/Medicare
eligibility check and when a missing tool call is injected, so a false
positive starts an interview nobody asked for and a miss leaves the prompt's
own instruction in charge. Both directions are pinned here, along with the
state it reads off the person's own words and the call it builds.
"""

import re
from pathlib import Path

import pytest

from fighthealthinsurance.chat.eligibility_intent import (
    ELIGIBILITY_INTERVIEW_NOTE,
    ELIGIBILITY_REQUEST_NOTE,
    build_eligibility_call,
    detect_eligibility_request,
    state_from_user_messages,
    state_mentioned_in,
    states_mentioned_in,
)
from fighthealthinsurance.chat.tools.patterns import (
    MEDICAID_ELIGIBILITY_REGEX,
    TOOL_DETECT_FLAGS,
    count_tool_invocations,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CHAT_INTERFACE_TSX = (
    REPO_ROOT / "fighthealthinsurance" / "static" / "js" / "chat_interface.tsx"
)


def _landing_page_first_message() -> str:
    """The message the /medicaid-eligibility landing page's CTA sends as the
    chat's first turn, read from the React source so the detector is tested
    against what the page actually says."""
    source = CHAT_INTERFACE_TSX.read_text()
    messages = re.findall(r"initialMessage = `([^`]+)`", source)
    for message in messages:
        if "Medicaid" in message:
            return message
    raise AssertionError("no Medicaid initialMessage in chat_interface.tsx")


@pytest.mark.parametrize(
    "message, state",
    [
        ("Am I eligible for Medicaid?", None),
        ("do i qualify for medi-cal", "ca"),
        ("Would my mom qualify for Medicare? She's in Texas.", "tx"),
        ("Not sure if I'm eligible for medicaid, I live in Ohio", "oh"),
        ("Can you see if we qualify for CHIP?", None),
        ("Will I lose my Medicaid when the work requirements start?", None),
        ("Can I get Medicaid? I'm 30, single, in CA", "ca"),
        ("Do the new work requirements apply to me?", None),
        ("What's my eligibility for MassHealth?", "ma"),
        ("Run the Medicaid eligibility check please", None),
        ("Is my patient eligible for AHCCCS?", "az"),
        ("Could my 70 year old mother be eligible for Medicare?", None),
        ("I'm wondering whether I would still qualify for Apple Health", "wa"),
        ("am i going to get kicked off medicaid", None),
    ],
)
def test_requests_are_detected_with_their_state(message, state):
    request = detect_eligibility_request(message)
    assert request is not None, message
    assert request.state == state


def test_the_landing_page_first_message_is_a_request():
    # The /medicaid-eligibility CTA promises a check; its first message has
    # to read as a request or the funnel depends on the model noticing.
    assert detect_eligibility_request(_landing_page_first_message()) is not None


@pytest.mark.parametrize(
    "message",
    [
        "What is Medicaid?",
        "What are the income limits for Medi-Cal?",
        "How do I apply for Medicaid in Texas?",
        "Am I eligible to appeal this denial?",
        "Am I eligible for Medicaid coverage of a wheelchair?",
        "Can I get Medicaid to pay for my MRI?",
        "Can you do an eligibility check for this patient's BlueCross plan?",
        "Do I qualify for STAR?",
        "Do I qualify for medical assistance with my hospital bills?",
        "My Medicaid was denied, help me appeal",
        "The insurer says I'm not eligible for the procedure",
        "",
    ],
)
def test_general_and_unrelated_questions_are_not_requests(message):
    assert detect_eligibility_request(message) is None


def test_none_is_not_a_request():
    assert detect_eligibility_request(None) is None


@pytest.mark.parametrize(
    "text, state",
    [
        ("I'm in CA", "ca"),
        ("Los Angeles, CA", "ca"),
        ("I live in ca", None),  # lower case is too often a word
        ("in or near Boston", None),
        ("I moved from Texas to California last year", None),  # two states
        ("Medi-Cal", "ca"),
        ("I'm on BadgerCare Plus", "wi"),
        ("New York", "ny"),
        ("Washington DC", "dc"),
        ("state of Washington", "wa"),
        ("I'm 30 and single", None),
        ("", None),
    ],
)
def test_state_mentioned_in(text, state):
    assert state_mentioned_in(text) == state


def test_state_from_user_messages_takes_the_newest_mention():
    # Newest first, as the caller hands them over.
    assert state_from_user_messages(["I'm 30", "I'm in Texas", "in Ohio"]) == "tx"
    assert state_from_user_messages(["I'm 30", None, ""]) is None


def test_state_from_user_messages_stops_at_an_ambiguous_message():
    # The newest message names two states: ambiguous, and the older "I live
    # in Texas" must not be reached past it -- Texas may be the state they
    # just left.
    assert (
        state_from_user_messages(["I moved from Texas to California", "I live in Texas"])
        is None
    )


def test_states_mentioned_in_lists_every_state():
    assert states_mentioned_in("I moved from Texas to California") == {"tx", "ca"}
    assert states_mentioned_in("Medi-Cal, in CA") == {"ca"}
    assert states_mentioned_in("I'm 30 and single") == frozenset()
    assert states_mentioned_in(None) == frozenset()


def test_built_call_carries_the_state_and_is_what_the_tool_detects():
    with_state = build_eligibility_call("ca")
    without = build_eligibility_call(None)
    assert with_state == '**medicaid_eligibility {"state": "ca"}**'
    assert without == "**medicaid_eligibility {}**"
    for call in (with_state, without):
        assert re.search(MEDICAID_ELIGIBILITY_REGEX, call, TOOL_DETECT_FLAGS)
        assert count_tool_invocations(call) == 1


def test_notes_name_the_tool_and_forbid_guessing():
    for note in (ELIGIBILITY_REQUEST_NOTE, ELIGIBILITY_INTERVIEW_NOTE):
        assert note.startswith("\n\n[Note from the system, not the user:")
        assert "medicaid_eligibility" in note
        assert "guess a verdict" in note
        assert "EXPERIMENTAL" in note
