"""Improve Arena review insights: the vote's interaction summary is cleaned
leniently, so a vote is never lost over it."""

from fighthealthinsurance.chooser_review_signals import (
    MAX_COUNT,
    clean_review_signals,
)


def test_well_formed_signals_are_kept():
    raw = {
        "first_interaction_ms": 1200,
        "pointer_moves": 340,
        "pointer_downs": 4,
        "key_presses": 0,
        "scroll_events": 12,
        "selection_changes": 2,
        "hidden_count": 1,
        "round_in_page": 3,
        "viewport_width": 1280,
        "pointer_types": ["mouse"],
        "vote_input": "pointer",
        "webdriver": False,
        "overflowing_positions": [1, 0],
        "scrolled_to_end_positions": [1],
    }

    assert clean_review_signals(raw) == {**raw, "overflowing_positions": [0, 1]}


def test_non_dict_cleans_to_empty():
    assert clean_review_signals(["pointer_moves", 3]) == {}


def test_unknown_keys_are_ignored():
    assert clean_review_signals({"favorite_color": "blue"}) == {}


def test_missing_and_null_keys_are_left_out():
    assert clean_review_signals({"pointer_moves": None, "key_presses": 2}) == {
        "key_presses": 2
    }


def test_malformed_values_are_named_under_dropped():
    cleaned = clean_review_signals(
        {
            "pointer_moves": "lots",
            "key_presses": -1,
            "webdriver": "no",
            "vote_input": "telepathy",
            "pointer_types": ["mouse", "laser"],
            "overflowing_positions": [0, 99],
            "scroll_events": 5,
        }
    )

    assert cleaned == {
        "scroll_events": 5,
        "dropped": [
            "pointer_moves",
            "key_presses",
            "overflowing_positions",
            "pointer_types",
            "vote_input",
            "webdriver",
        ],
    }


def test_huge_counts_are_clamped():
    assert clean_review_signals({"pointer_moves": 10**12}) == {
        "pointer_moves": MAX_COUNT
    }


def test_booleans_are_not_counts():
    assert clean_review_signals({"key_presses": True}) == {"dropped": ["key_presses"]}


def test_duplicate_pointer_types_are_collapsed():
    assert clean_review_signals({"pointer_types": ["touch", "mouse", "touch"]}) == {
        "pointer_types": ["mouse", "touch"]
    }
