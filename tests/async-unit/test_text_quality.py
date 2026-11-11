"""text_quality.is_garbled: real plan text passes, the garbage a PDF's text
layer can read as fails."""

import pytest

from fighthealthinsurance import text_quality
from tests.plan_text_samples import (
    CHINESE_TEXT,
    CONTRACT_TEXT,
    FORMULARY_TEXT,
    PLAN_TEXT,
    RATE_TABLE_TEXT,
    SPANISH_TEXT,
    TABLE_TEXT,
    dots_and_symbols,
    private_use_characters,
    shifted,
    spaced,
)

REAL = {
    "plan summary": PLAN_TEXT,
    "table of benefits": TABLE_TEXT,
    "contract language": CONTRACT_TEXT,
    "drug formulary": FORMULARY_TEXT,
    "Spanish": SPANISH_TEXT,
    "rate table": RATE_TABLE_TEXT,
    "short title page": "Evidence of Coverage 2027",
    "another script": CHINESE_TEXT,
    "summary and table": PLAN_TEXT + "\n" + TABLE_TEXT,
}
GARBAGE = {
    "dots and symbols": dots_and_symbols(),
    "private-use characters": private_use_characters(),
    "letters shifted by one": shifted(PLAN_TEXT + TABLE_TEXT, 1),
    "letters shifted by three": shifted(PLAN_TEXT + TABLE_TEXT, 3),
    "letters shifted back by two": shifted(PLAN_TEXT + TABLE_TEXT, -2),
    "shifted formulary": shifted(FORMULARY_TEXT + CONTRACT_TEXT, 3),
    "spaced-out letters": spaced(PLAN_TEXT),
    "spaced-out shifted letters": spaced(shifted(PLAN_TEXT, 3)),
    "nothing but spaces": " \n\t ",
}


@pytest.mark.parametrize("text", REAL.values(), ids=REAL.keys())
def test_real_plan_text_is_not_garbled(text):
    assert not text_quality.is_garbled(text)


@pytest.mark.parametrize("text", GARBAGE.values(), ids=GARBAGE.keys())
def test_garbage_is_garbled(text):
    assert text_quality.is_garbled(text)


def test_real_text_clears_the_threshold_with_room_to_spare():
    for name in ("plan summary", "table of benefits", "contract language", "Spanish"):
        assert text_quality.common_word_share(REAL[name]) > 2 * text_quality.MIN_COMMON_WORD_SHARE


def test_shifted_text_stays_well_under_the_threshold():
    for by in (1, 3, 13, -2):
        share = text_quality.common_word_share(shifted(PLAN_TEXT + TABLE_TEXT, by))
        assert share < text_quality.MIN_COMMON_WORD_SHARE / 2


def test_a_garbled_page_after_a_clean_one_is_still_judged_on_the_whole():
    # The sample spreads over the whole text, so a clean opening can't carry it.
    text = PLAN_TEXT + "\n" + shifted(PLAN_TEXT * 40, 3)
    assert text_quality.is_garbled(text)


def test_a_long_text_is_judged_on_a_bounded_sample():
    words = text_quality._sample_words(PLAN_TEXT * 2000)
    assert len(words) == text_quality.SAMPLE_WORDS
