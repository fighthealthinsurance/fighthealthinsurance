// What the person typed (their name, their email, their street) found in a
// letter and taken out of it. Used by Remove personal details
// (scrub_scrub.ts) and by the chat's scrubPersonalInfo (user_info_storage.ts).
//
// A value is matched as typed, the way main matched it: split into words at
// its spaces, punctuation and all (a typed "123 Main St , Apt 4B" has a word
// ","), each word escaped, any run of whitespace in the letter between them
// taken as equal (a space, two, a tab, a line break, a non-breaking space:
// a letter often puts "Apt 4B" on the line under the street), and case
// ignored. Nothing is trimmed off its ends, so a typed initial "A." is
// looked for as "A.", not as "A".
//
// Matched anywhere, a typed "M" turned "Example Health Plan" into
// "Exa {{FIRST_NAME}}ple Health Plan" in a live test. So where a match sits
// is checked against the edges of words.
//
// Word characters: a letter of a script with capitals (Latin, Greek,
// Cyrillic, Armenian, ...), a digit, and a mark on one of those. Between
// two characters there is a word edge unless both are word characters. An
// apostrophe, a hyphen, a period or an underscore is not a word character,
// so "Brien" is a word of "O'Brien" and "Smith" of "Smith-Jones". Nor is a
// letter of a script without capitals (Chinese, Japanese, Korean, Thai,
// Arabic, Hebrew, ...): those put no space between words or join particles
// and prefixes to a name ("김민수님", "وعلي"), so a value written in them is
// found anywhere, as main found it, and a Latin name or an email right
// against them ("患者Ann Doe的申请") is found too.
//
// A value of one word ("Ann", "A.", "#4B", "62701") is taken out only where
// it stands whole: a word edge at both ends of the match. "Ann" stays in
// "annual" and "M" in "Example".
//
// A value of more words ("Ann Doe", "283 24th St", "Smith-Jones",
// "ann@example.com", "62701-1234"; each run of word characters is a word,
// and so is each letter of a script without capitals) is taken out wherever
// main found it. The words inside it are whole already, since there is an
// edge at each space or punctuation mark between them. Where its first word
// runs on to the left in the letter, or its last word to the right, the
// match goes on to the end of the word the letter prints: a typed
// "283 24th St" takes out all of "283 24th Street", and a typed "Ann Doe"
// all of "Joann Doe". So nothing of it that main took out is left.
//
// A placeholder the site puts in text ({{PATIENT_NAME}}, {{SCSID}},
// {{Your Phone Number}}, ...) is never matched into: a last name "Name"
// leaves {{PATIENT_NAME}} as it is. Any other text in double braces is
// plain text, and a name in it comes out.
//
// Every value is looked for in the text as it is, before any is taken out
// (takeOutTypedValues), so taking one out never hides another. Taken out
// one after another, a first name inside the street ("77 Ann St") broke the
// street up and left the rest of it.
//
// A value with no letter or digit is not looked for at all (null): it would
// match all over the letter and holds nothing of the person.
//
// Safari before 16.4 (iOS 15 and early iOS 16) cannot read a lookbehind,
// and a pattern it cannot read throws. So the edges are not in the pattern:
// typedValueMatches tests them from where each match starts and ends.

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

const LETTER_OR_DIGIT = new RegExp("[\\p{L}\\p{N}]", "u");
const LETTER = new RegExp("^\\p{L}$", "u");
const CASED_LETTER = new RegExp("^[\\p{Lu}\\p{Ll}\\p{Lt}]$", "u");
const DIGIT = new RegExp("^\\p{Nd}$", "u");
const MARK = new RegExp("^\\p{M}$", "u");

// The placeholders the site puts in text: the scrubbers' own, the label
// rules', and those the letters and replies are written with.
const SITE_PLACEHOLDER = new RegExp(
  "\\{\\{(?:[A-Z][A-Z0-9_ ]*|Your Name|Your Email Address|Your Phone Number|Your Address|" +
    "date|today|insurance_company|patient_name|patient_dob|provider_name|provider_npi|" +
    "practice_name|practice_address)\\}\\}",
  "g",
);
const ANY_PLACEHOLDER = /\{\{[^{}]*\}\}/g;

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

// A letter of a script with capitals, or a digit.
function casedLetterOrDigit(character: string): boolean {
  return (
    CASED_LETTER.test(character) ||
    DIGIT.test(character) ||
    (LETTER.test(character) && character.toUpperCase() !== character.toLowerCase())
  );
}

// Whether the character that ends at index is a word character: a mark is
// one where the letter or digit it sits on is.
function wordCharacterBefore(text: string, index: number): boolean {
  let at = index;
  while (at > 0) {
    const character = characterBefore(text, at);
    if (!MARK.test(character)) {
      return casedLetterOrDigit(character);
    }
    at -= character.length;
  }
  return false;
}

// Whether the character that starts at index is a word character.
function wordCharacterAt(text: string, index: number): boolean {
  const character = characterAt(text, index);
  if (character === "") {
    return false;
  }
  return MARK.test(character) ? wordCharacterBefore(text, index) : casedLetterOrDigit(character);
}

// Whether there is a word edge at index: unless the characters either side
// of it are both word characters.
function wordEdgeAt(text: string, index: number): boolean {
  return !(wordCharacterBefore(text, index) && wordCharacterAt(text, index));
}

// How many words a value has: each run of word characters is one, and so
// is each letter of a script without capitals.
function wordsIn(value: string): number {
  let words = 0;
  for (let i = 0; i < value.length; ) {
    const character = characterAt(value, i);
    if (wordCharacterAt(value, i)) {
      if (wordEdgeAt(value, i)) {
        words++;
      }
    } else if (LETTER.test(character)) {
      words++;
    }
    i += character.length;
  }
  return words;
}

// What a typed value is looked for as.
export interface TypedValue {
  // The value as main matched it: each word escaped, any whitespace between.
  pattern: RegExp;
  // Whether it has more than one word, so that its first word may run on to
  // the left and its last word to the right.
  runsOn: boolean;
}

// The value as it is looked for, or null where it is not (see above).
export function typedValue(value: string): TypedValue | null {
  if (!LETTER_OR_DIGIT.test(value)) {
    return null;
  }
  const words = value.split(/\s+/).filter((word) => word !== "");
  return {
    pattern: new RegExp(words.map(escapeRegExp).join("\\s+"), "gi"),
    runsOn: wordsIn(value) > 1,
  };
}

// Where each placeholder of the site is in the text, as [start, end), and
// each of the others given (the ones a scrubber puts in).
function placeholdersIn(text: string, others: string[]): [number, number][] {
  const spans: [number, number][] = [];
  SITE_PLACEHOLDER.lastIndex = 0;
  let found: RegExpExecArray | null;
  while ((found = SITE_PLACEHOLDER.exec(text)) !== null) {
    spans.push([found.index, found.index + found[0].length]);
  }
  for (let i = 0; i < others.length; i++) {
    for (let at = text.indexOf(others[i]); at >= 0; at = text.indexOf(others[i], at + 1)) {
      spans.push([at, at + others[i].length]);
    }
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

// Every place the value is taken out of the text, as [start, end),
// overlapping ones too: after a match the search goes on from the next
// character, so a match that is left does not hide one that starts inside
// it. A match that runs into a placeholder is left, and one of a value of
// one word that is not whole; one of more words is carried on to the ends
// of the words it starts and ends inside.
export function typedValueMatches(
  text: string,
  typed: TypedValue,
  placeholders: [number, number][] = placeholdersIn(text, []),
): [number, number][] {
  const found: [number, number][] = [];
  const pattern = typed.pattern;
  pattern.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(text)) !== null) {
    let start = match.index;
    let end = start + match[0].length;
    pattern.lastIndex = start + 1;
    if (overlapsAny(placeholders, start, end)) {
      continue;
    }
    if (typed.runsOn) {
      while (!wordEdgeAt(text, start)) {
        start -= characterBefore(text, start).length;
      }
      while (!wordEdgeAt(text, end)) {
        end += characterAt(text, end).length;
      }
    } else if (!wordEdgeAt(text, start) || !wordEdgeAt(text, end)) {
      continue;
    }
    found.push([start, end]);
  }
  pattern.lastIndex = 0;
  return found;
}

// The text with every typed value taken out, each replaced by its
// placeholder. Every value is looked for in the text as it is, before any
// is taken out, so taking one out never hides another: a first name inside
// the street ("77 Ann St") or the email ("ann.doe@example.com") no longer
// breaks the street or the email up and leaves the rest of it. Neither a
// placeholder of the site nor one these values are replaced by is matched
// into. Where matches overlap, the whole run of them comes out, and in its
// place go the placeholders of the matches in it that are not inside
// another, in order: "Ann Doe", found as the full name and as each name,
// becomes {{PATIENT_NAME}}, and a street that runs into a city starting
// with the street's last word becomes "{{ADDRESS}} {{CITY}}". Of two
// matches of the same text, the value listed first gives the placeholder.
export function takeOutTypedValues(text: string, values: [TypedValue, string][]): string {
  const putIn: string[] = [];
  for (let i = 0; i < values.length; i++) {
    const tokens = values[i][1].match(ANY_PLACEHOLDER) || [];
    for (let j = 0; j < tokens.length; j++) {
      if (putIn.indexOf(tokens[j]) < 0) {
        putIn.push(tokens[j]);
      }
    }
  }
  const placeholders = placeholdersIn(text, putIn);
  const found: { start: number; end: number; order: number; placeholder: string }[] = [];
  for (let order = 0; order < values.length; order++) {
    const matches = typedValueMatches(text, values[order][0], placeholders);
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
