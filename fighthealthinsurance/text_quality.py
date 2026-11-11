"""Whether text read from a file is real text or garbage.

A PDF can carry a text layer that reads as garbage while its pages look fine:
fonts with encodings of their own give dots and symbols, or letters shifted
along the alphabet. A scan has no text layer at all. Both need OCR, and
neither may be stored as if it were the document's text.

The check is cheap and needs no model: real text in English or Spanish is
full of short common words ("the", "of", "and", "de", "la"), and a plan
document's tables still name plans, services and costs, while garbled text
has almost none. Text mostly in another script (Chinese, Korean, Russian...)
is not judged by its words, only by how much of it is symbols.
"""

import re
import unicodedata

#: Common words of English and Spanish, with the plain words of plan
#: documents. Single letters are left out: spaced-out garbage ("T h e")
#: is all single letters.
COMMON_WORDS = frozenset("""
    the of and to in is for on or be are as at by an it if not no you your
    we our us this that these those with from will may can must any all each
    other more than which who what when where how there their they them its
    has have had do does did been was were but also only under after before
    up out about into over per such some same so then would should could
    shall until within without both between during through because see use
    used get gets include includes including apply applies required page
    section call information about type types first year years day days
    plan plans health care coverage covered cover covers benefit benefits
    service services member members provider providers network deductible
    copay copayment coinsurance cost costs pay paid payment payments amount
    amounts insurance medical claim claims policy prescription drug drugs
    hospital emergency visit visits office limit limits pocket maximum prior
    authorization preferred specialist primary physician doctor treatment
    therapy surgery outpatient inpatient mental behavioral premium employer
    group individual family dependent dependents eligible enrollment appeal
    appeals denied denial notice rights review
    tier tiers generic brand specialty formulary pharmacy retail mail order
    supply quantity step tab tabs tablet tablets cap caps capsule capsules
    oral injection solution cream mg ml mcg er hcl pa ql st
    de la el que en los las del por para con una uno un su sus se es al lo
    como más mas este esta estos estas si sí le les ser son puede pueden
    usted cuando donde cual cuál sobre entre sin también tambien hay año
    plan cobertura servicios servicio beneficios médico medico médica medica
    atención atencion salud costo costos pago deducible copago coseguro
    proveedor proveedores red seguro reclamo visita miembro afiliado
    """.split())

#: The share of a text's words that must be common words for it to count as
#: real text. Plan text, tables of benefits included, runs well above it, and
#: text garbled by a font's own encoding sits near zero (both are tested).
MIN_COMMON_WORD_SHARE = 0.12
#: Below this many words a text is too short to judge by its words; it is
#: judged by how much of it is symbols instead.
MIN_WORDS_TO_JUDGE = 20
#: A short text is garbage when symbols (not letters, digits or spaces) make up
#: more than half of it and number at least this many: a page of dots.
MIN_SYMBOLS_FOR_GARBAGE = 200
#: The most words read to judge a text: enough for a steady share, and a
#: fixed cost however long the text.
SAMPLE_WORDS = 5000

_WORD = re.compile(r"[^\W\d_]+")
#: Letters past Latin Extended-B: other scripts, judged by symbols only.
_LATIN_END = 0x024F


def _is_symbol(ch: str) -> bool:
    """Visible, and neither a letter nor a digit; private-use and unassigned
    characters, which custom font encodings often give, count too."""
    if ch.isspace() or ch.isdigit():
        return False
    return not unicodedata.category(ch).startswith("L")


def _sample_words(text: str) -> list[str]:
    """Up to SAMPLE_WORDS words, spread over the whole text, lowercased."""
    found = _WORD.findall(text)
    if len(found) > SAMPLE_WORDS:
        step = len(found) / SAMPLE_WORDS
        found = [found[int(i * step)] for i in range(SAMPLE_WORDS)]
    return [w.lower() for w in found]


def common_word_share(text: str) -> float:
    """The share of the text's words (sampled) that are common words."""
    words = _sample_words(text)
    if not words:
        return 0.0
    return sum(1 for w in words if w in COMMON_WORDS) / len(words)


def is_garbled(text: str) -> bool:
    """True when ``text`` holds no real text: empty, mostly symbols, or words
    that are almost never common words. A short text with few symbols is not
    judged garbled: there is too little to tell."""
    visible = [ch for ch in text if not ch.isspace()]
    if not visible:
        return True
    words = _sample_words(text)
    if len(words) < MIN_WORDS_TO_JUDGE:
        symbols = sum(1 for ch in visible if _is_symbol(ch))
        return symbols >= MIN_SYMBOLS_FOR_GARBAGE and symbols * 2 > len(visible)
    letters = [ch for w in words for ch in w]
    other_script = sum(1 for ch in letters if ord(ch) > _LATIN_END)
    if other_script * 2 > len(letters):
        symbols = sum(1 for ch in visible if _is_symbol(ch))
        return symbols * 2 > len(visible)
    share = sum(1 for w in words if w in COMMON_WORDS) / len(words)
    return share < MIN_COMMON_WORD_SHARE
