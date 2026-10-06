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
const PUNCTUATION_AT_THE_ENDS = new RegExp(`^[^${WORD_CHARACTERS}]+|[^${WORD_CHARACTERS}]+$`, "gu");
const LETTERS_AND_DIGITS = new RegExp("[\\p{L}\\p{N}]", "gu");
const ONE_CHARACTER_NAME = new RegExp("^\\p{Lo}$", "u");

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// Null where the value is not looked for (see above). Replace with
// replaceTypedValue, not String.replace: a match starts with the character
// before the value, which has to stay.
export function typedValueRegExp(value: string): RegExp | null {
  const trimmed = value.replace(PUNCTUATION_AT_THE_ENDS, "");
  const lettersAndDigits = trimmed.match(LETTERS_AND_DIGITS) || [];
  if (lettersAndDigits.length === 0) {
    return null;
  }
  if (lettersAndDigits.length === 1 && !ONE_CHARACTER_NAME.test(lettersAndDigits[0])) {
    return null;
  }
  const words = trimmed
    .split(/\s+/)
    .filter((word) => word !== "")
    .map((word) => escapeRegExp(word).replace(/['’]/g, APOSTROPHE));
  return new RegExp(BEFORE + "(" + words.join("\\s+") + ")" + AFTER, "giu");
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
