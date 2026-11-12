"""Whether text read from a file is real text or garbage.

A PDF can carry a text layer that reads as garbage while its pages look fine:
fonts with encodings of their own give dots and symbols, or letters shifted
along the alphabet. A scan has no text layer at all. Both need OCR, and
neither may be stored as if it were the document's text.

The check is cheap and needs no model: real text in English or Spanish is
full of short common words ("the", "of", "and", "de", "la"), and a plan
document's tables still name plans, services and costs, while garbled text
has almost none. Text with few common words is garbage when shifting its
letters back turns it into English or Spanish (a font's own encoding often
shifts every letter by the same amount), or when it doesn't have the shape
of written language: mostly symbols, or letters standing alone. Otherwise
it is taken as a language the word list doesn't know (Vietnamese, Tagalog)
and passes, so a real plan is never lost to the word list. Text mostly in
another script (Chinese, Korean, Russian...) is judged by symbols only.
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
#: Text is garbage when symbols (not letters, digits or spaces) make up more
#: than half of it and number at least this many: a page of dots, whatever
#: words are mixed in.
MIN_SYMBOLS_FOR_GARBAGE = 200
#: With no words at all, this many symbols are enough; fewer, as in a page
#: number between dashes, is too little to tell.
MIN_SYMBOLS_WITHOUT_WORDS = 20
#: The most characters counted for symbols, taken from across the text.
SYMBOL_SAMPLE_CHARS = 50_000
#: The most words read to judge a text: enough for a steady share, and a
#: fixed cost however long the text.
SAMPLE_WORDS = 5000
#: The most characters tried shifted back, taken from across the text.
SHIFT_SAMPLE_CHARS = 8000
#: Shifted back, garbage reads as plainly as the text it came from (plan
#: text runs 0.3 to 0.75); a real language with short words, such as
#: Tagalog, reaches about 0.16 by chance on its best shift.
MIN_SHIFTED_BACK_SHARE = 0.25
#: Written language, in any Latin-script language: at most this share of
#: symbols and of one-letter words, and words this long on average.
MAX_SYMBOL_SHARE = 0.25
MAX_ONE_LETTER_SHARE = 0.25
MIN_MEAN_WORD_LENGTH = 2.5
MAX_MEAN_WORD_LENGTH = 12.0

_WORD = re.compile(r"[^\W\d_]+")
#: A run of one symbol, spaced or not: the dot leaders of a table of contents.
_LEADER = re.compile(r"([^\w\s])(?: ?\1){3,}")
#: Letters past Latin Extended-B: other scripts, judged by symbols only.
_LATIN_END = 0x024F


def _is_symbol(ch: str) -> bool:
    """Visible, and neither a letter nor a digit; private-use and unassigned
    characters, which custom font encodings often give, count too."""
    if ch.isspace() or ch.isdigit():
        return False
    return not unicodedata.category(ch).startswith("L")


def _shift_tables() -> list[dict[int, int]]:
    """Each way back from a shifted text: the alphabet rotated by 1 to 25,
    and printable ASCII moved by 1 to 64 either way."""
    tables = []
    for by in range(1, 26):
        table = {}
        for first in (ord("a"), ord("A")):
            for i in range(26):
                table[first + i] = first + (i + by) % 26
        tables.append(table)
    for by in [*range(1, 65), *range(-64, 0)]:
        tables.append(
            {code: code + by for code in range(33, 127) if 33 <= code + by <= 126}
        )
    return tables


_SHIFTS = _shift_tables()


def _spread(text: str, size: int, pieces: int = 8) -> str:
    """Up to ``size`` characters, in ``pieces`` windows spread over ``text``."""
    if len(text) <= size:
        return text
    width, step = size // pieces, len(text) // pieces
    return " ".join(text[i * step : i * step + width] for i in range(pieces))


def _reads_when_shifted_back(text: str) -> bool:
    sample = _spread(text, SHIFT_SAMPLE_CHARS)
    return any(
        common_word_share(sample.translate(table)) >= MIN_SHIFTED_BACK_SHARE
        for table in _SHIFTS
    )


def _has_the_shape_of_language(words: list[str], symbol_share: float) -> bool:
    one_letter = sum(1 for w in words if len(w) == 1)
    mean_length = sum(len(w) for w in words) / len(words)
    return (
        symbol_share <= MAX_SYMBOL_SHARE
        and one_letter <= MAX_ONE_LETTER_SHARE * len(words)
        and MIN_MEAN_WORD_LENGTH <= mean_length <= MAX_MEAN_WORD_LENGTH
    )


def _without_leaders(text: str) -> str:
    """Each run of one symbol cut to one, on lines that also have letters: a
    contents line ("Your costs ........ 12") is not a line of symbols."""
    return "\n".join(
        _LEADER.sub(r"\1", line) if any(ch.isalpha() for ch in line) else line
        for line in text.split("\n")
    )


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
    that are almost never common words and either read as English or Spanish
    once shifted back or don't have the shape of written language. A short
    text with some words and few symbols is not judged garbled: there is too
    little to tell."""
    sample = _without_leaders(_spread(text, SYMBOL_SAMPLE_CHARS))
    visible = sum(1 for ch in sample if not ch.isspace())
    if not visible:
        return True
    symbols = sum(1 for ch in sample if _is_symbol(ch))
    words = _sample_words(text)
    if symbols * 2 > visible and (
        symbols >= MIN_SYMBOLS_FOR_GARBAGE
        or (not words and symbols >= MIN_SYMBOLS_WITHOUT_WORDS)
    ):
        return True
    if len(words) < MIN_WORDS_TO_JUDGE:
        return False
    letters = [ch for w in words for ch in w]
    if sum(1 for ch in letters if ord(ch) > _LATIN_END) * 2 > len(letters):
        return False
    share = sum(1 for w in words if w in COMMON_WORDS) / len(words)
    if share >= MIN_COMMON_WORD_SHARE:
        return False
    if _reads_when_shifted_back(text):
        return True
    return not _has_the_shape_of_language(words, symbols / visible)


def is_clearly_text(text: str) -> bool:
    """Enough words to judge, and not garbled: a page worth keeping as it is
    even when the text around it is garbage."""
    return len(_sample_words(text)) >= MIN_WORDS_TO_JUDGE and not is_garbled(text)
