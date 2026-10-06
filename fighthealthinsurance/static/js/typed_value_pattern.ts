// A value the person typed (their name, their street) as a regular
// expression that finds it in a letter however the letter spaces it, and
// only where it stands as whole words, and the replace that puts a
// placeholder in its place. Used by Remove personal details (scrub_scrub.ts)
// and by the chat's scrubPersonalInfo (user_info_storage.ts).
//
// Spacing: each word is escaped, and any run of whitespace between words
// (spaces, a tab, a line break) is taken as equal. A letter often puts "Apt
// 4B" on the line under "123 Sample Street", so a typed "123 Sample Street
// Apt 4B" matched with its literal spaces was left in the letter.
//
// Whole words: a letter or a digit, in any script, right before or right
// after the value stops the match. Matched anywhere, a typed "Ann" took the
// "ann" out of "annual" and an "Ed" the "ed" out of "denied", and in a live
// test a short first name turned "Example Health Plan" into "Exa
// {{FIRST_NAME}}ple Health Plan" before the letter was drafted from. \b knows
// only A to Z, so it would never end a match after the "é" of "José" and
// would let "Jos" match inside "José". The edges are tested over Unicode
// letters, the marks that sit on them, and digits (the u flag): after the
// value with a lookahead; before it by matching the one character there (or
// the start of the text) and putting it back, which is what
// replaceTypedValue is for. A lookbehind would say it more simply, but
// Safari before 16.4 (iOS 15 and early iOS 16) cannot read one, and a
// pattern it cannot read throws.
//
// Apostrophes: an apostrophe joined to a word is part of it. A typed "Brien"
// is not taken out of "O'Brien", nor "Don" out of "don't", but "Ann's" loses
// its "Ann". A straight apostrophe typed in the value also finds the curly
// one a letter prints (O’Brien). A hyphen is not part of a word: a typed
// "Smith" is taken out of "Smith-Jones", which is still the person's name.
//
// Ends: punctuation at either end of what was typed ("Jr.", "4B,") is left
// off, so the edges are tested where the value's letters start and stop, and
// "123 Sample St." finds "123 Sample St" as well.
//
// Scripts without capitals: Chinese, Japanese and Thai put no space between
// words, and Korean, Arabic and Hebrew join particles and prefixes to a name
// ("김민수님께", "ومحمد"). So there is no edge on a side of the value that
// starts or ends with a letter that has no capital form: 王小明 is found in
// "患者王小明的申请", as it was before whole words. That also takes the 王
// out of 王国, but in an English letter these scripts are almost always the
// name, and too much taken out is the safe side. Latin, Cyrillic and Greek
// letters have capitals and keep both edges.
//
// What kind of value it is loosens the edge after it (TypedValueKind):
// - "street": a letter often prints the street longer than it was typed
//   (the intake page's own hint is "283 24th St"). Its last word runs on to
//   the end of the word the letter has, so "St" finds "Street" and "Apt 4"
//   finds "Apt 4B". A suffix, unit or direction anywhere in it finds its
//   other form too ("Street" finds "ST", "Rd" finds "ROAD", "N" finds
//   "NORTH"), and a period or comma after a word inside it may or may not be
//   printed. A street starts with its house number, which keeps its edge, so
//   this does not take ordinary words.
// - "zip": a five-digit ZIP code finds the four digits a letter prints
//   after it ("62701-1234", "62701 1234", "627011234"), and a typed ZIP+4
//   finds the five digits alone. A longer number with the ZIP inside it
//   ("claim 627012") is still left.
//
// fullNameRegExp finds the first and last name together where the letter
// prints a longer form of the typed first name: "Chris" and "Doe" find
// "Christopher Doe" and "DOE, CHRISTOPHER". On its own the first name is
// still a whole word, so "Ed" leaves "Edward" and "Ann" leaves "annual".
// The longer form has to start with a capital, so "Sam" and "Price" leave
// "the same price".
//
// Not looked for at all (null):
// - a value with no letters or digits, which would match everywhere;
// - a lone initial: one letter or digit. As a whole word it is also "a",
//   "I", Medicare's "Part B" and every "1." in a list, so taking it out
//   would wreck the letter, and an initial on its own says almost nothing
//   about who someone is. A single character from a script without
//   capitals, like the surname 王 or 김, is a whole name and turns up in an
//   English letter only as that name, so it is still looked for.

// What words are made of, in a character class with the u flag: letters in
// any script, the marks that sit on them, and digits.
export const WORD_CHARACTERS = "\\p{L}\\p{M}\\p{N}";
const LETTER_OR_DIGIT = `[${WORD_CHARACTERS}]`;
const APOSTROPHE = "['\\u2019]";
// The start of the text or a character that is not part of a word, and an
// apostrophe after it if there is one: so not right after a letter or
// digit, nor after one and an apostrophe.
const BEFORE = `((?:^|[^${WORD_CHARACTERS}'\\u2019])${APOSTROPHE}?)`;
// Not before a letter or digit, nor before an apostrophe and one, except
// the possessive "'s".
const AFTER = `(?!${LETTER_OR_DIGIT})(?!${APOSTROPHE}(?!s(?!${LETTER_OR_DIGIT}))${LETTER_OR_DIGIT})`;
// Matched in a word instead of AFTER: the rest of the word, however long.
const RUN_ON = `[${WORD_CHARACTERS}]*`;
const PUNCTUATION_AT_THE_ENDS = new RegExp(`^[^${WORD_CHARACTERS}]+|[^${WORD_CHARACTERS}]+$`, "gu");
const LETTERS_AND_DIGITS = new RegExp("[\\p{L}\\p{N}]", "gu");
const ONE_CHARACTER_NAME = new RegExp("^\\p{Lo}$", "u");
// A letter with no capital form at the start or the end of a word (the marks
// on it included): Han, kana, Hangul, Thai, Arabic, Hebrew and the like.
const CASED_LETTER = "[\\p{Lu}\\p{Ll}\\p{Lt}]";
const STARTS_CASELESS = new RegExp(`^(?!${CASED_LETTER})\\p{L}`, "u");
const ENDS_CASELESS = new RegExp(`(?!${CASED_LETTER})\\p{L}\\p{M}*$`, "u");
// A ZIP code as typed, and the four digits a letter may print after one.
const ZIP_CODE = /^(\d{5})(?:[-\s]?\d{4})?$/;
const PLUS_FOUR = "(?:[- ]?\\d{4})?";

// The forms of a word in a street that a letter may print in place of the
// one typed, lower case. Each word of a typed street is looked up here.
const STREET_WORD_FORMS: string[][] = [
  ["st", "street"],
  ["ave", "av", "avenue"],
  ["rd", "road"],
  ["dr", "drive"],
  ["ln", "lane"],
  ["blvd", "boulevard"],
  ["ct", "court"],
  ["pl", "place"],
  ["cir", "circle"],
  ["hwy", "highway"],
  ["pkwy", "parkway"],
  ["ter", "terrace"],
  ["apt", "apartment"],
  ["ste", "suite"],
  ["n", "north"],
  ["s", "south"],
  ["e", "east"],
  ["w", "west"],
  ["ne", "northeast"],
  ["nw", "northwest"],
  ["se", "southeast"],
  ["sw", "southwest"],
];

export type TypedValueKind = "words" | "street" | "zip";
export type NameOrder = "first last" | "last, first";

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// The words of what was typed, without punctuation at its ends, or null
// where it is not looked for (see above).
function typedWords(value: string): string[] | null {
  const trimmed = value.replace(PUNCTUATION_AT_THE_ENDS, "");
  const lettersAndDigits = trimmed.match(LETTERS_AND_DIGITS) || [];
  if (lettersAndDigits.length === 0) {
    return null;
  }
  if (lettersAndDigits.length === 1 && !ONE_CHARACTER_NAME.test(lettersAndDigits[0])) {
    return null;
  }
  return trimmed.split(/\s+/).filter((word) => word !== "");
}

function wordPattern(word: string): string {
  return escapeRegExp(word).replace(/['’]/g, APOSTROPHE);
}

// The word in either case, letter by letter, for a regular expression made
// without the i flag; with capitalFirst, its first letter only as a capital.
// (With the i flag, \p{Lu} matches small letters too, so a capital cannot be
// asked for any other way.)
function eitherCase(word: string, capitalFirst: boolean): string {
  return Array.from(word)
    .map((character, i) => {
      const lower = character.toLowerCase();
      const upper = character.toUpperCase();
      if (lower === upper) {
        return wordPattern(character);
      }
      if (i === 0 && capitalFirst) {
        return escapeRegExp(upper);
      }
      return "(?:" + escapeRegExp(lower) + "|" + escapeRegExp(upper) + ")";
    })
    .join("");
}

function streetWordForms(word: string): string[] | null {
  const lower = word.toLowerCase();
  for (const forms of STREET_WORD_FORMS) {
    if (forms.indexOf(lower) >= 0) {
      return forms;
    }
  }
  return null;
}

// A typed street: every word as typed or in another form of it, a period or
// comma after any word but the last, and the last word running on (unless
// it ends in a script without capitals, which has no edge after it to run
// past and would take the rest of the sentence with it).
function streetPattern(words: string[]): string {
  const bare = words.map((word) => word.replace(/[.,]+$/, "")).filter((word) => word !== "");
  return bare
    .map((word, i) => {
      const forms = streetWordForms(word);
      const pattern = forms === null ? wordPattern(word) : "(?:" + forms.join("|") + ")";
      return i === bare.length - 1 && !ENDS_CASELESS.test(word) ? pattern + RUN_ON : pattern;
    })
    .join("[.,]*\\s+");
}

// The pattern with the edges around it, except on a side where its text
// starts or ends with a letter that has no capital form.
function withEdges(firstWord: string, lastWord: string, pattern: string, flags: string): RegExp {
  const before = STARTS_CASELESS.test(firstWord) ? "()" : BEFORE;
  const after = ENDS_CASELESS.test(lastWord) ? "" : AFTER;
  return new RegExp(before + "(" + pattern + ")" + after, flags);
}

// Null where the value is not looked for (see above). Replace with
// replaceTypedValue, not String.replace: a match starts with the character
// before the value, which has to stay.
export function typedValueRegExp(value: string, kind: TypedValueKind = "words"): RegExp | null {
  const words = typedWords(value);
  if (words === null) {
    return null;
  }
  const first = words[0];
  const last = words[words.length - 1];
  if (kind === "zip") {
    const zip = ZIP_CODE.exec(words.join(" "));
    if (zip !== null) {
      return withEdges(first, last, zip[1] + PLUS_FOUR, "giu");
    }
  }
  if (kind === "street") {
    return withEdges(first, last, streetPattern(words), "giu");
  }
  return withEdges(first, last, words.map(wordPattern).join("\\s+"), "giu");
}

// The first and last name together, in the order given, with the first
// name's last word running on to the end of the word the letter prints and
// starting with a capital there (see above). Null where either name is not
// looked for, or where the first name ends in a script without capitals,
// which typedValueRegExp already finds inside a longer word. Replace with
// replaceTypedValue.
export function fullNameRegExp(firstName: string, lastName: string, order: NameOrder): RegExp | null {
  const first = typedWords(firstName);
  const last = typedWords(lastName);
  if (first === null || last === null || ENDS_CASELESS.test(first[first.length - 1])) {
    return null;
  }
  const firstPattern =
    first.map((word, i) => eitherCase(word, i === first.length - 1)).join("\\s+") + "[\\p{L}\\p{M}]*";
  const lastPattern = last.map((word) => eitherCase(word, false)).join("\\s+");
  return order === "first last"
    ? withEdges(first[0], last[last.length - 1], firstPattern + "\\s+" + lastPattern, "gu")
    : withEdges(last[0], first[first.length - 1], lastPattern + "\\s*,\\s*" + firstPattern, "gu");
}

// The text with each match of a typedValueRegExp replaced: by the
// placeholder, or by what a function makes of the value as the text has it.
export function replaceTypedValue(
  text: string,
  typed: RegExp,
  replacement: string | ((found: string) => string),
): string {
  return text.replace(typed, (_match: string, before: string, found: string) =>
    before + (typeof replacement === "string" ? replacement : replacement(found)),
  );
}
