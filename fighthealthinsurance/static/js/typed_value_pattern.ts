// What the person typed (their name, their street) found in a letter, and
// taken out of it where it stands as whole words. Used by Remove personal
// details (scrub_scrub.ts) and by the chat's scrubPersonalInfo
// (user_info_storage.ts).
//
// Spacing: each word is escaped, and any run of whitespace between words
// (spaces, a tab, a line break, a non-breaking space) is taken as equal. A
// letter often puts "Apt 4B" on the line under "123 Sample Street", so a
// typed "123 Sample Street Apt 4B" matched with its literal spaces was left
// in the letter. A word is whatever was typed between spaces, punctuation
// and all, so a typed "123 Main St , Apt 4B" finds the same text.
//
// Whole words: matched anywhere, a typed "Ann" took the "ann" out of
// "annual" and an "Ed" the "ed" out of "denied", and in a live test a typed
// "M" turned "Example Health Plan" into "Exa {{FIRST_NAME}}ple Health Plan"
// before the letter was drafted from. So a match is left where it sits
// inside a longer word: where the character right before it or right after
// it is part of the same word as the match's own first or last character
// (inOneWord). That is the one thing matching anywhere took out that this
// leaves in. Part of the same word:
// - letters that have capitals (Latin, Greek, Cyrillic, ...) and digits run
//   together: "Ann" stays in "Joann", "Annie" and "Ann2";
// - a mark is part of the letter it sits on: "Jose" stays in a "José"
//   written with a combining accent;
// - but each letter of a script without capitals is a word to itself.
//   Chinese, Japanese and Thai put no space between words, and Korean,
//   Arabic and Hebrew join particles and prefixes to a name ("김민수님께",
//   "ومحمد"), so 王小明 is still taken out of "患者王小明的申请" (and the
//   王 out of 王国, as matching anywhere did);
// - and a change between such a script and one with capitals or a digit is
//   the edge of a word: "Ann Doe" is taken out of "患者Ann Doe的申请", an
//   email out of "我的电子邮箱是ann@example.com。".
// Nothing else is part of a word. An apostrophe, a hyphen, a period or an
// underscore is not, so "Brien" is taken out of "O'Brien", "Smith" out of
// "Smith-Jones" and an email out of "first.ann@example.com", as before.
// A lone initial is looked for too: as a whole word, a typed "A" takes the
// "a" out of "a plan", where matching anywhere took it out of every word.
//
// Safari before 16.4 (iOS 15 and early iOS 16) cannot read a lookbehind,
// and a pattern it cannot read throws. So the edges are not in the pattern:
// typedValueMatches tests them from where each match starts and ends.
//
// Every value is looked for in the text as it is, before any is taken out
// (takeOutTypedValues), so taking one out never hides another, and a
// {{PLACEHOLDER}} in the text is passed over whole. Taken out one after
// another, a first name inside the street ("77 Ann St") broke the street up
// and left the rest of it, and a last name "Name" turned "{{PATIENT_NAME}}"
// into "{{PATIENT_{{LAST_NAME}}}}".
//
// Also: punctuation at either end of what was typed ("Jr.", "4B,") is left
// off, so a typed "123 Sample St." finds "123 Sample St" as well; a
// straight apostrophe typed finds the curly one a letter prints (O’Brien),
// and the other way round; and a value with no letter or digit is not
// looked for at all (null), since it would match all over the letter.

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

const PUNCTUATION_AT_THE_ENDS = new RegExp("^[^\\p{L}\\p{M}\\p{N}]+|[^\\p{L}\\p{M}\\p{N}]+$", "gu");
const LETTER_OR_DIGIT = new RegExp("[\\p{L}\\p{N}]", "u");
const APOSTROPHES = /['\u2019]/g;
const PLACEHOLDER = "\\{\\{[^{}]*\\}\\}";

const CASED_OR_DIGIT = new RegExp("^[\\p{Lu}\\p{Ll}\\p{Lt}\\p{N}]$", "u");
const ANY_LETTER = new RegExp("^\\p{L}$", "u");
const ANY_MARK = new RegExp("^\\p{M}$", "u");

// What a character is to a word: a letter with a capital form or a digit
// ("cased"), a letter of a script without capitals ("caseless"), a mark, or
// not part of a word (null).
type CharacterKind = "cased" | "caseless" | "mark" | null;

function kindOf(character: string): CharacterKind {
  if (character === "") {
    return null;
  }
  if (ANY_MARK.test(character)) {
    return "mark";
  }
  if (CASED_OR_DIGIT.test(character)) {
    return "cased";
  }
  return ANY_LETTER.test(character) ? "caseless" : null;
}

// The character (a whole code point) that starts at index, or "".
function characterAt(text: string, index: number): string {
  return index < text.length ? String.fromCodePoint(text.codePointAt(index) as number) : "";
}

// The character (a whole code point) that ends at index, or "".
function characterBefore(text: string, index: number): string {
  if (index <= 0) {
    return "";
  }
  const low = text.charCodeAt(index - 1);
  if (low >= 0xdc00 && low <= 0xdfff && index >= 2) {
    const high = text.charCodeAt(index - 2);
    if (high >= 0xd800 && high <= 0xdbff) {
      return text.slice(index - 2, index);
    }
  }
  return text.charAt(index - 1);
}

// The kind of the letter or digit before index, through any marks on it.
function kindBefore(text: string, index: number): CharacterKind {
  let at = index;
  while (at > 0) {
    const character = characterBefore(text, at);
    const kind = kindOf(character);
    if (kind !== "mark") {
      return kind;
    }
    at -= character.length;
  }
  return null;
}

// Whether the characters either side of index are part of one word (see
// above).
function inOneWord(text: string, index: number): boolean {
  const after = kindOf(characterAt(text, index));
  if (after === null) {
    return false;
  }
  const before = kindBefore(text, index);
  if (before === null) {
    return false;
  }
  return after === "mark" || (before === "cased" && after === "cased");
}

// Where each {{PLACEHOLDER}} in the text is, as [start, end).
function placeholdersIn(text: string): [number, number][] {
  const spans: [number, number][] = [];
  const placeholder = new RegExp(PLACEHOLDER, "g");
  let found: RegExpExecArray | null;
  while ((found = placeholder.exec(text)) !== null) {
    spans.push([found.index, found.index + found[0].length]);
  }
  return spans;
}

function overlapsAny(spans: [number, number][], start: number, end: number): boolean {
  for (let i = 0; i < spans.length; i++) {
    if (spans[i][0] < end && spans[i][1] > start) {
      return true;
    }
  }
  return false;
}

// The value as a pattern, or null where it is not looked for (see above).
// The pattern also matches every {{PLACEHOLDER}}, in its first group, so
// the search passes over each one whole. Use with typedValueMatches or
// takeOutTypedValues, which leave those, and a match inside a longer word.
export function typedValueRegExp(value: string): RegExp | null {
  const trimmed = value.replace(PUNCTUATION_AT_THE_ENDS, "");
  if (!LETTER_OR_DIGIT.test(trimmed)) {
    return null;
  }
  const words = trimmed.split(/\s+/).filter((word) => word !== "");
  const pattern = words.map((word) => escapeRegExp(word).replace(APOSTROPHES, "['\u2019]")).join("\\s+");
  return new RegExp("(" + PLACEHOLDER + ")|" + pattern, "giu");
}

// Every place the value stands as whole words in the text, as [start,
// end), overlapping ones too. After a match the search goes on from the
// next character, so a match that is not whole words (or one that is) does
// not hide another that starts inside it.
export function typedValueMatches(text: string, typed: RegExp): [number, number][] {
  const placeholders = placeholdersIn(text);
  const found: [number, number][] = [];
  typed.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = typed.exec(text)) !== null) {
    if (match[1] !== undefined) {
      // A placeholder: the search goes on after it.
      continue;
    }
    const start = match.index;
    const end = start + match[0].length;
    if (!inOneWord(text, start) && !inOneWord(text, end) && !overlapsAny(placeholders, start, end)) {
      found.push([start, end]);
    }
    typed.lastIndex = start + characterAt(text, start).length;
  }
  typed.lastIndex = 0;
  return found;
}

// The text with every typed value taken out where it stands as whole
// words, each replaced by its placeholder. Every value is looked for in the
// text as it is, before any is taken out, so taking one out never hides
// another: a first name inside the street ("77 Ann St") or the email
// ("ann.doe@example.com") no longer breaks the street or the email up and
// leaves the rest of it. Where matches overlap, the whole run of them comes
// out, and in its place go the placeholders of the matches in it that are
// not inside another, in order: "Ann Doe", found as the full name and as
// each name, becomes {{PATIENT_NAME}}, and a street that runs into a city
// starting with the street's last word becomes "{{ADDRESS}} {{CITY}}". Of
// two matches of the same text, the value listed first gives the
// placeholder.
export function takeOutTypedValues(text: string, values: [RegExp, string][]): string {
  const found: { start: number; end: number; order: number; placeholder: string }[] = [];
  for (let order = 0; order < values.length; order++) {
    const matches = typedValueMatches(text, values[order][0]);
    for (let i = 0; i < matches.length; i++) {
      found.push({ start: matches[i][0], end: matches[i][1], order, placeholder: values[order][1] });
    }
  }
  found.sort((a, b) => a.start - b.start || b.end - a.end || a.order - b.order);
  // Runs of overlapping matches, each with the placeholders of the matches
  // that start it or carry it further.
  const runs: { start: number; end: number; placeholders: string[] }[] = [];
  for (let i = 0; i < found.length; i++) {
    const last = runs[runs.length - 1];
    if (last !== undefined && found[i].start < last.end) {
      if (found[i].end > last.end) {
        last.end = found[i].end;
        last.placeholders.push(found[i].placeholder);
      }
    } else {
      runs.push({ start: found[i].start, end: found[i].end, placeholders: [found[i].placeholder] });
    }
  }
  if (runs.length === 0) {
    return text;
  }
  // Each character stays, or starts a run and becomes its placeholders, or
  // is the rest of a run and goes.
  let next = 0;
  return text.replace(/[\s\S]/g, (character: string, offset: number) => {
    while (next < runs.length && runs[next].end <= offset) {
      next++;
    }
    const run = runs[next];
    if (run === undefined || offset < run.start) {
      return character;
    }
    return offset === run.start ? run.placeholders.join(" ") : "";
  });
}
