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


OTHER_LANGUAGES = {
    "German": "Die Krankenversicherung übernimmt medizinisch notwendige Behandlungen. " * 4,
    "French": (
        "Le résumé des garanties et de la couverture vous aide à choisir un régime "
        "d'assurance maladie. Il montre comment vous et le régime partagez le coût "
        "des soins médicaux couverts."
    ),
    "Tagalog": (
        "Ang Buod ng mga Benepisyo at Saklaw ay tumutulong sa iyo na pumili ng "
        "planong pangkalusugan. Ipinapakita nito kung paano kayo ng plano maghahati "
        "sa gastos ng mga sakop na serbisyo sa pangangalagang pangkalusugan."
    ),
    "Vietnamese": (
        "Bản Tóm tắt Quyền lợi và Bảo hiểm giúp quý vị chọn một chương trình bảo hiểm "
        "sức khỏe. Bản này cho thấy quý vị và chương trình sẽ chia sẻ chi phí cho các "
        "dịch vụ chăm sóc sức khỏe được bảo hiểm như thế nào."
    ),
}


@pytest.mark.parametrize("text", OTHER_LANGUAGES.values(), ids=OTHER_LANGUAGES.keys())
def test_a_real_plan_in_a_language_the_word_list_lacks_is_not_garbled(text):
    assert not text_quality.is_garbled(text)


@pytest.mark.parametrize("by", [b for b in range(-25, 26) if b])
def test_every_shift_of_the_alphabet_is_garbled(by):
    assert text_quality.is_garbled(shifted(PLAN_TEXT + TABLE_TEXT, by))


def _moved(text, by):
    return "".join(
        chr(ord(ch) + by) if 33 <= ord(ch) <= 126 and 33 <= ord(ch) + by <= 126 else ch
        for ch in text
    )


@pytest.mark.parametrize("by", [-29, -10, -3, -1, 1, 3, 10])
def test_printable_characters_moved_by_a_fixed_amount_are_garbled(by):
    assert text_quality.is_garbled(_moved(PLAN_TEXT + TABLE_TEXT, by))


def test_symbols_are_garbage_whatever_words_come_with_them():
    assert text_quality.is_garbled("the " * 20 + "\n" + "." * 12000)
    assert text_quality.is_garbled("." * 199)


def test_a_contents_page_with_dot_leaders_is_not_garbled():
    contents = "\n".join(
        f"Section {i}. {title} {'.' * 40} {i * 3}"
        for i, title in enumerate(
            ["Your costs", "Covered services", "Exclusions", "Claims and appeals",
             "Definitions", "Prescription drugs", "Mental health care"], 1)
    )
    assert not text_quality.is_garbled(contents)


def test_a_page_number_between_dashes_is_too_little_to_judge():
    assert not text_quality.is_garbled("- 3 -")


def test_a_page_is_clearly_text_only_with_enough_words():
    assert text_quality.is_clearly_text(PLAN_TEXT)
    assert not text_quality.is_clearly_text("Evidence of Coverage 2027")
    assert not text_quality.is_clearly_text(shifted(PLAN_TEXT, 3))


def test_real_word_count_is_none_for_garbage():
    assert text_quality.real_word_count(shifted(PLAN_TEXT, 3)) == 0
    assert text_quality.real_word_count("Evidence of Coverage 2027") == 3
