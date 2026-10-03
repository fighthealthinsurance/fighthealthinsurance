// The person's own name, street and ZIP code, read from the denial letter in
// the box and put into the About you fields they have left empty. The
// reading happens on this page, and nothing here sends or logs what it
// found. What happens to a filled field after that is what happens to a
// typed one: the name and street fields have no form name, so they stay in
// this browser, but the ZIP field is part of the form, so a ZIP filled here
// is sent with it (the server keeps the first three digits and works out
// the state from them).
//
// A denial letter carries several addresses (the insurer's return address,
// often the provider's, and the member's), so the rule is narrow on purpose
// and fills nothing rather than guess:
//
// 1. The name. The salutation ("Dear Jordan Example,") and the labels
//    "Member:", "Member name:", "Patient:", "Patient name:", "Subscriber:"
//    and "Subscriber name:" name the person, up to a tab or two spaces (the
//    next column) or a word that starts another label. A generic salutation
//    ("Dear Member", "Dear Sir or Madam", "Dear Provider") names no one, and
//    nor does one to a doctor ("Dear Dr. Pat Provider", "Dear Casey
//    Doctorson, MD"). Only a name that splits cleanly counts: a first name,
//    any middle initials, and a last name ("Example, Jordan A" reads the
//    same way). A middle name spelled out fills nothing, and so do two
//    different people anywhere among the salutations and labels. Two middle
//    initials that differ (Jordan A. Example and Jordan B. Example) are two
//    people; a name with an initial and the same name without one are the
//    same.
// 2. The name has to be backed up. A salutation alone is not enough: a
//    greeting this does not know ("Dear Card Holder") reads like a name. A
//    label in capitals with no comma is not enough either, since it could
//    be EXAMPLE JORDAN as well as JORDAN EXAMPLE. Either counts with an
//    address block under the name, or with the other one agreeing. Any
//    other label counts on its own.
// 3. The address. Only from a block whose first line is that same name: the
//    name line, then a street line (a number and street words, or a PO box),
//    an optional apartment or unit line, then a "City, ST 12345" line, all
//    single spaced or all double spaced. Never from a block under a heading
//    for something else ("Services for:", "Provider:"), in any column of the
//    line above it, and never under a name line with a different middle
//    initial or a middle name spelled out. The name, street and unit lines
//    are read only up to a wide gap (a tab, or three or more spaces): what
//    is past it is another column, such as "Member ID:" on the street
//    line's baseline. The city line is read whole, with any gap between the
//    city, the state and the ZIP (a monospaced PDF can leave the ZIP a tab
//    away), up to the end of the line or a wide gap past the ZIP. The
//    street line and the five-digit ZIP come from that block and no other.
//    With a unit line, or a unit in the column beside the street, only the
//    ZIP is filled: the street field would drop the unit, so the finished
//    appeal would lose it and Remove personal details would leave it in the
//    letter. Two such blocks that disagree fill no address.
// 4. Only empty fields, and each one once. A name the person typed (or that
//    came back from storage) that is not the letter's means the letter is
//    about someone else, so nothing is filled. The street and ZIP go
//    together: the ZIP goes in only with the letter's street, filled from
//    the same block in the same pass or already in the street field, or,
//    for a block with a unit line, beside a street field that is still
//    empty and not the person's. A field this filled, one the person has
//    typed in or emptied, and one that already held something when the
//    page set up (restored from this browser, or sent back by the server)
//    are the person's from then on: empty or not, a later paste does not
//    fill them. The email is never filled.
//
// The fill is a convenience, so it never stops the page: a browser whose
// storage is full still gets the field filled and its hint, the value just
// is not remembered. (Storage the browser blocks reads as "Remember what I
// typed" turned off, in shared.ts, so nothing is written there.)
//
// A name the letter shouts in capitals is put in as Jordan Example; any
// other casing is kept as the letter has it.

export interface LetterDetails {
  firstName?: string;
  lastName?: string;
  street?: string;
  zip?: string;
}

interface PersonName {
  first: string;
  last: string;
  // The middle initials, in capitals without dots ("A" for "A."), or "".
  middle: string;
}

// Written under each field this fills, and by the About you heading.
export const FROM_LETTER_HINT = "From your letter. Please check.";
export const FILLED_NOTE = "We filled in what we found in your letter.";

// The fields this fills, in page order. Never the email: letters rarely
// carry it, and a guess there would send someone else's address.
const FIELDS: Array<[keyof LetterDetails, string]> = [
  ["firstName", "store_fname"],
  ["lastName", "store_lname"],
  ["street", "store_street"],
  ["zip", "store_zip"],
];

const LETTER = "A-Za-z\\u00C0-\\u00D6\\u00D8-\\u00F6\\u00F8-\\u024F";
const NAME_WORD = new RegExp("^[" + LETTER + "][" + LETTER + "'\\u2019\\-]*$");
const INITIAL = /^[A-Za-z]\.?$/;

// Words that make a salutation or a label generic, or show it is about a
// provider, a company or a team rather than a person. Kept to words no one
// is named, so "May" and "Holder" still count.
const NOT_A_NAME = new Set([
  "and", "or", "of", "the", "to", "whom", "concern", "for", "our", "your",
  "member", "members", "valued", "sir", "madam", "provider", "providers", "patient",
  "patients", "customer", "subscriber", "policyholder", "cardholder", "beneficiary",
  "enrollee", "participant", "applicant", "insured", "guardian", "colleague", "doctor",
  "dr", "physician", "prescriber", "nurse", "team", "staff", "office", "plan", "health",
  "insurance", "benefit", "benefits", "claim", "claims", "department", "dept", "services",
  "appeals", "representative", "authorized", "administrator", "manager", "coordinator",
  "specialist", "director", "reviewer", "facility", "pharmacy", "hospital", "medical",
  "clinic", "inc", "llc", "company", "corporation", "account", "id", "dob", "policy",
  "covered", "recipient", "practitioner", "healthcare", "professional", "committee",
  "utilization", "management", "medicaid", "medicare",
]);
// A label's value ends at the next word that starts another label.
const ENDS_A_LABEL = new Set([
  "id", "dob", "date", "birth", "member", "patient", "subscriber", "group", "claim",
  "account", "number", "no", "plan", "provider", "policy", "reference", "ref", "case",
  "phone", "tel", "address", "sex", "gender", "age",
]);
const COURTESY_TITLES = new Set(["mr", "mrs", "ms", "miss", "mx"]);
// A letter to a doctor is the provider's, not the person's.
const DOCTOR_TITLES = new Set(["dr", "doctor"]);
// A clinician's credential after the comma, as in "Dear Casey Doctorson,
// MD:", marks a letter to the provider the way "Dr." does. Capitals only, so
// "Dear Jordan Example, do call us" is still to Jordan.
const CREDENTIAL = new RegExp(
  "^\\s*(?:M\\.?D|D\\.?O|N\\.?P|P\\.?A(?:-C)?|R\\.?N|APRN|FNP(?:-[A-Z]{1,3})?|DNP|CRNA|CNM|" +
    "DPM|DDS|DMD|O\\.?D|D\\.?C|Ph\\.?D|PharmD|PsyD|LCSW|LMFT|LPC|MSW|MBBS)\\b",
);
const SUFFIXES = new Set(["jr", "sr", "ii", "iii", "iv"]);
// Parts of a last name that come before it: Maria de la Cruz.
const PARTICLES = new Set([
  "de", "del", "della", "der", "di", "da", "dos", "das", "du", "la", "le", "van", "von", "st",
]);

// The postal codes of the states, DC, the territories and the military
// mail regions, so "Room 12" or a stray pair of capitals is not a state.
const STATES = new Set(
  (
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC " +
    "ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY AS GU MP PR VI AA AE AP"
  ).split(" "),
);

function bare(word: string): string {
  return word.toLowerCase().replace(/[.,]/g, "");
}

function words(text: string): string[] {
  return text.split(/\s+/).filter((word) => word !== "");
}

// A wide gap on a line: a tab (pdf_text.ts keeps a PDF's wide gaps as one),
// or three or more spaces in pasted text. Past it is another column. Two
// spaces are not one: OCR leaves them inside a street.
const COLUMN_GAP = /\t|[ \u00A0]{3,}/;
// A label's value ends sooner, at a tab or two spaces: in "Member name:
// Jordan Example  Service Date: 09/01/2026" the name stops at "Example".
const LABEL_GAP = /\t|\s{2,}/;

// A line's columns, from the left, each with its spaces made single.
function columnsOf(text: string, gap: RegExp = COLUMN_GAP): string[] {
  return text
    .split(gap)
    .map((column) => column.replace(/\s+/g, " ").trim())
    .filter((column) => column !== "");
}

function isInitial(word: string): boolean {
  return INITIAL.test(word);
}

function initialsOf(tokens: string[]): string {
  return tokens.map((token) => token.replace(/\./g, "").toUpperCase()).join("");
}

function isNameWord(word: string): boolean {
  return NAME_WORD.test(word) && word.length > 1;
}

function startsCapitalised(word: string): boolean {
  const first = word.charAt(0);
  return first === first.toUpperCase() && first !== first.toLowerCase();
}

function titleCase(text: string): string {
  return text
    .toLowerCase()
    .replace(
      /(^|[\s'\u2019\-])([a-z\u00DF-\u00F6\u00F8-\u00FF])/g,
      (_, before: string, letter: string) => before + letter.toUpperCase(),
    );
}

// Takes off a courtesy title in front and a suffix behind. Null for a
// doctor's title: that letter is to the provider.
function withoutTitles(tokens: string[]): string[] | null {
  let rest = tokens.slice();
  if (rest.length > 0 && DOCTOR_TITLES.has(bare(rest[0]))) {
    return null;
  }
  if (rest.length > 0 && COURTESY_TITLES.has(bare(rest[0]))) {
    rest = rest.slice(1);
  }
  if (rest.length > 0 && SUFFIXES.has(bare(rest[rest.length - 1]))) {
    rest = rest.slice(0, -1);
  }
  return rest;
}

// A first name, any middle initials, and a last name (with any particles in
// front of it), from "Jordan A. Example" or "Example, Jordan A". Null for
// anything else, including a middle name spelled out: there is no telling
// that from a two-word last name.
function parseName(raw: string): PersonName | null {
  const text = raw.replace(/\s+/g, " ").trim().replace(/[\s.,;:]+$/, "");
  const parts = text.split(",").map((part) => part.trim());
  let firstTokens: string[];
  let lastTokens: string[];
  // After a comma the last name is everything before it; without one, only
  // particles (de, van) can join the last word.
  let lastNameMarked = false;
  if (parts.length === 1) {
    const tokens = withoutTitles(words(parts[0]));
    if (tokens === null || tokens.length < 2) {
      return null;
    }
    const lastWord = tokens.length - 1;
    let start = lastWord;
    while (start > 1 && PARTICLES.has(bare(tokens[start - 1]))) {
      start -= 1;
    }
    firstTokens = tokens.slice(0, start);
    lastTokens = tokens.slice(start);
  } else if (parts.length === 2 && words(parts[1]).length === 1 && SUFFIXES.has(bare(parts[1]))) {
    // "Jordan Example, Jr."
    return parseName(parts[0]);
  } else if (parts.length === 2) {
    const after = withoutTitles(words(parts[1]));
    if (after === null) {
      return null;
    }
    firstTokens = after;
    lastTokens = words(parts[0]);
    lastNameMarked = true;
  } else {
    return null;
  }
  if (firstTokens.length === 0 || lastTokens.length === 0 || lastTokens.length > 4) {
    return null;
  }
  const first = firstTokens[0];
  const middles = firstTokens.slice(1);
  const surname = lastTokens[lastTokens.length - 1];
  const beforeSurname = lastTokens.slice(0, -1);
  if (!isNameWord(first) || !isNameWord(surname) || !middles.every(isInitial)) {
    return null;
  }
  const joinsTheSurname = (word: string): boolean =>
    PARTICLES.has(bare(word)) || (lastNameMarked && isNameWord(word) && startsCapitalised(word));
  if (!beforeSurname.every(joinsTheSurname)) {
    return null;
  }
  const everyWord = firstTokens.concat(lastTokens);
  if (everyWord.some((word) => NOT_A_NAME.has(bare(word)))) {
    return null;
  }
  // A name is written with capitals; "we reviewed" after a stray label is
  // not one.
  if (!startsCapitalised(first) || !startsCapitalised(surname)) {
    return null;
  }
  const last = lastTokens.join(" ");
  const whole = first + " " + last;
  const shouting = whole === whole.toUpperCase();
  const middle = initialsOf(middles);
  return shouting ? { first: titleCase(first), last: titleCase(last), middle } : { first, last, middle };
}

function sameWords(a: string, b: string): boolean {
  return a.replace(/\s+/g, " ").trim().toLowerCase() === b.replace(/\s+/g, " ").trim().toLowerCase();
}

// Middle initials that are both there and differ are two people; one name
// with an initial and one without can be the same person.
function sameMiddle(a: string, b: string): boolean {
  return a === "" || b === "" || a === b;
}

function samePerson(a: PersonName, b: PersonName): boolean {
  return sameWords(a.first, b.first) && sameWords(a.last, b.last) && sameMiddle(a.middle, b.middle);
}

// The one person the list names, or null when it names none or two. Every
// pair has to agree: Jordan Example agrees with Jordan A. Example and with
// Jordan B. Example, but those two are not one person.
function onePerson(names: PersonName[]): PersonName | null {
  if (names.length === 0) {
    return null;
  }
  for (let i = 0; i < names.length; i += 1) {
    for (let j = i + 1; j < names.length; j += 1) {
      if (!samePerson(names[i], names[j])) {
        return null;
      }
    }
  }
  const withInitial = names.filter((name) => name.middle !== "");
  return withInitial.length > 0 ? withInitial[0] : names[0];
}

function namesFromSalutations(text: string): PersonName[] {
  const found: PersonName[] = [];
  // To the comma or colon after it, or the end of its line.
  const salutation = /\bdear\s+([^,:;\n]{1,60}?)\s*(?:[,:;]|$)/gim;
  let match: RegExpExecArray | null;
  while ((match = salutation.exec(text)) !== null) {
    if (match[0].endsWith(",") && CREDENTIAL.test(text.slice(salutation.lastIndex))) {
      continue;
    }
    const name = parseName(match[1]);
    if (name !== null) {
      found.push(name);
    }
  }
  return found;
}

interface LabelledName {
  name: PersonName;
  // False for a name in capitals with no comma: EXAMPLE JORDAN reads as
  // well one way round as the other.
  orderIsClear: boolean;
}

function namesFromLabels(lines: string[]): LabelledName[] {
  const found: LabelledName[] = [];
  const label = /(^|[^A-Za-z])(?:member|patient|subscriber)(?:\s+name)?\s*:/gi;
  for (const line of lines) {
    label.lastIndex = 0;
    let match: RegExpExecArray | null;
    while ((match = label.exec(line)) !== null) {
      // "Dear Member: ..." is a salutation, not a label.
      if (/\bdear\s*$/i.test(line.slice(0, match.index + match[1].length))) {
        continue;
      }
      // The value runs to a gap (a tab or two spaces: the next column), then
      // to the first word that is not part of a name. A gap between the
      // label and its value is not one: that is the label's own column.
      const value = columnsOf(line.slice(match.index + match[0].length), LABEL_GAP)[0] ?? "";
      const taken: string[] = [];
      for (const word of words(value)) {
        const plain = word.replace(/,$/, "");
        const nameLike = isNameWord(plain) || isInitial(plain) || COURTESY_TITLES.has(bare(plain));
        if (/[0-9:]/.test(word) || ENDS_A_LABEL.has(bare(plain)) || !nameLike) {
          break;
        }
        taken.push(word);
        if (taken.length === 6) {
          break;
        }
      }
      const raw = taken.join(" ");
      const name = parseName(raw);
      if (name !== null) {
        found.push({ name, orderIsClear: raw.includes(",") || raw !== raw.toUpperCase() });
      }
    }
  }
  return found;
}

// Whether a line of an address block is this person's name: their first
// name first and their last name last, with only name words between.
function isNameLine(line: string, person: PersonName): boolean {
  if (/[0-9:]/.test(line)) {
    return false;
  }
  const parts = line.split(",").map((part) => part.trim());
  let tokens: string[] | null;
  if (parts.length === 1) {
    tokens = withoutTitles(words(parts[0]));
  } else if (parts.length === 2) {
    // "Example, Jordan A" reads as "Jordan A Example".
    const after = withoutTitles(words(parts[1]));
    tokens = after === null ? null : after.concat(words(parts[0]));
  } else {
    return false;
  }
  if (tokens === null || tokens.length < 2 || tokens.length > 6) {
    return false;
  }
  if (!tokens.every((word) => isNameWord(word.replace(/\.$/, "")) || isInitial(word))) {
    return false;
  }
  const lastLength = words(person.last).length;
  if (tokens.length < 1 + lastLength) {
    return false;
  }
  if (!sameWords(tokens[0], person.first) || !sameWords(tokens.slice(-lastLength).join(" "), person.last)) {
    return false;
  }
  // Between the first and last names only initials that agree: a
  // different initial, or a middle name spelled out (Jordan Bob Example for
  // Jordan A. Example), is someone else's block, or cannot be told from one.
  const between = tokens.slice(1, tokens.length - lastLength);
  return between.every(isInitial) && sameMiddle(initialsOf(between), person.middle);
}

const STREET = new RegExp(
  "^\\d+[A-Za-z]?(?:[-/]\\d+[A-Za-z]?)?\\s+(?:[NSEW]\\.?\\s+)?[" + LETTER + "0-9][^\\n]*$",
);
const PO_BOX = /^(?:P\.?\s*O\.?\s*Box|Post\s+Office\s+Box)\s+\d+/i;
const UNIT = /^(?:apt|apartment|unit|suite|ste|#|bldg|building|floor|fl|room|rm)\b/i;
// A city line, matched across the whole line: any whitespace, a tab
// included, can sit between the city, the state and the ZIP, since a
// monospaced PDF can put "62701" two spaces past "Springfield, IL" and the
// gap comes through as a tab. The city's own words are a space or two apart
// (wider is a column gap, and past it is another column), and the ZIP ends
// the line or comes before a wide gap.
const CITY_NAME = "[A-Za-z][A-Za-z.'\\u2019\\-]*(?:[ \\u00A0]{1,2}[A-Za-z.'\\u2019\\-]+)*";
const CITY_LINE = new RegExp(
  "^(" + CITY_NAME + ")(?:\\s*,\\s*|\\s+)([A-Za-z]{2})\\.?\\s+(\\d{5})(?:-\\d{4})?(?=$|\\t|[ \\u00A0]{3,})",
);

function isStreetLine(line: string): boolean {
  if (line.length > 60 || CITY_LINE.test(line)) {
    return false;
  }
  // A number and at least one real word after it: not an ID or a date.
  return (STREET.test(line) && /[A-Za-z]{2,}/.test(line.replace(/^\S+/, ""))) || PO_BOX.test(line);
}

function zipFromCityLine(line: string): string | null {
  const match = CITY_LINE.exec(line);
  if (match === null || !STATES.has(match[2].toUpperCase())) {
    return null;
  }
  return match[3];
}

interface Address {
  // None when the block has a unit line (see the rule at the top).
  street?: string;
  zip: string;
}

// A heading for something other than the person: the block under it is
// that heading's, a claim's services or a provider's, not a mailing block.
const ANOTHER_PARTYS_HEADING = /(?:\bfor|\bprovider|\bfacility)\s*:$/i;

// Whether the nearest line above with anything on it has such a heading in
// any of its columns: "Member ID: XYZ000000<gap>Services for:" heads the
// block under it as surely as "Services for:" alone does.
function underAnotherPartysHeading(columns: string[][], at: number): boolean {
  let above = at - 1;
  while (above >= 0 && columns[above].length === 0) {
    above -= 1;
  }
  return above >= 0 && columns[above].some((column) => ANOTHER_PARTYS_HEADING.test(column));
}

// `lines` are the letter's lines and `columns` each one's columns (see
// columnsOf). The name, street and unit lines are read up to their first
// wide gap, and the city line whole (see CITY_LINE).
function addressesFor(lines: string[], columns: string[][], person: PersonName): Address[] {
  const found: Address[] = [];
  const firstColumn = (index: number): string => columns[index][0] ?? "";
  for (let at = 0; at < lines.length; at += 1) {
    if (!isNameLine(firstColumn(at), person) || underAnotherPartysHeading(columns, at)) {
      continue;
    }
    // A reading can double-space a block, so a block is single spaced or
    // double spaced the whole way down. A mix is more likely a name line
    // and someone else's address run together.
    for (const step of [1, 2]) {
      // Where the block's nth line under the name is, or null.
      const blockLine = (n: number): number | null => {
        const index = at + n * step;
        if (index >= lines.length || firstColumn(index) === "") {
          return null;
        }
        return step === 2 && firstColumn(index - 1) !== "" ? null : index;
      };
      const streetAt = blockLine(1);
      if (streetAt === null || !isStreetLine(firstColumn(streetAt))) {
        continue;
      }
      const street = firstColumn(streetAt);
      let cityAt = blockLine(2);
      // "123 Sample Street<gap>Apt 4B" carries its unit beside it.
      const unitBeside = UNIT.test(columns[streetAt][1] ?? "");
      const unit = unitBeside || (cityAt !== null && UNIT.test(firstColumn(cityAt)));
      if (unit && !unitBeside) {
        cityAt = blockLine(3);
      }
      const zip = cityAt === null ? null : zipFromCityLine(lines[cityAt]);
      if (zip !== null) {
        found.push(unit ? { zip } : { street, zip });
      }
    }
  }
  return found;
}

// What the letter says about the person it is addressed to, by the rule at
// the top of this file. Empty when it does not say for sure.
export function findDetailsInLetter(text: string): LetterDetails {
  const lines = text
    .split(/\r?\n/)
    .map((line) => line.replace(/[ \t\u00A0]+$/, "").replace(/^[ \t\u00A0]+/, ""));
  const salutations = namesFromSalutations(text);
  const labels = namesFromLabels(lines);
  // Everyone the letter is to or about. Two different people: leave it to
  // them.
  const person = onePerson(salutations.concat(labels.map((label) => label.name)));
  if (person === null) {
    return {};
  }
  // Each line's columns, with single spaces, for matching (the label pass
  // above and the city line need the gaps).
  const columns = lines.map((line) => columnsOf(line));
  const addresses = addressesFor(lines, columns, person);
  const backedUp =
    addresses.length > 0 ||
    labels.some((label) => label.orderIsClear) ||
    (salutations.length > 0 && labels.length > 0);
  if (!backedUp) {
    return {};
  }
  const details: LetterDetails = { firstName: person.first, lastName: person.last };
  const agreed = addresses.every(
    (address) =>
      sameWords(address.street ?? "", addresses[0].street ?? "") && address.zip === addresses[0].zip,
  );
  if (addresses.length > 0 && agreed) {
    if (addresses[0].street !== undefined) {
      details.street = addresses[0].street;
    }
    details.zip = addresses[0].zip;
  }
  return details;
}

// Points a field at its hint (or stops), leaving any other description.
function describedBy(field: HTMLElement, id: string, on: boolean): void {
  const ids = (field.getAttribute("aria-describedby") ?? "")
    .split(/\s+/)
    .filter((token) => token !== "" && token !== id);
  if (on) {
    ids.push(id);
  }
  if (ids.length > 0) {
    field.setAttribute("aria-describedby", ids.join(" "));
  } else {
    field.removeAttribute("aria-describedby");
  }
}

function hintId(field: HTMLElement): string {
  return field.id + "_from_letter";
}

// Takes a field's hint down. False when it had none.
function removeHint(field: HTMLElement): boolean {
  const hint = document.getElementById(hintId(field));
  if (hint === null) {
    return false;
  }
  if (hint.parentNode !== null) {
    hint.parentNode.removeChild(hint);
  }
  describedBy(field, hintId(field), false);
  return true;
}

// The note by the About you heading shows while any field still carries its
// hint, and goes once the person has been through them all.
function updateNote(): void {
  const note = document.getElementById("details_from_letter");
  if (note === null) {
    return;
  }
  const anyHint = FIELDS.some(([, id]) => document.getElementById(id + "_from_letter") !== null);
  if (anyHint) {
    if (note.textContent !== FILLED_NOTE) {
      note.textContent = FILLED_NOTE;
    }
    note.hidden = false;
  } else {
    note.textContent = "";
    note.hidden = true;
  }
}

// Fields whose input events are followed.
const followedFields = new Set<string>();
// Fields this page never fills (rule 4): ones the letter has filled, ones
// the person has typed in or emptied, and ones that held something when
// the page set up. One they empty stays empty when page two of the letter
// is pasted under page one.
const leaveAlone = new Set<string>();

// Any input event on a field is the person's: the fill sets a value, which
// fires none. Their edit is also their check, so the hint goes at the
// first change.
function follow(field: HTMLInputElement): void {
  if (followedFields.has(field.id)) {
    return;
  }
  followedFields.add(field.id);
  field.addEventListener("input", () => {
    leaveAlone.add(field.id);
    if (removeHint(field)) {
      updateNote();
    }
  });
}

function markFromLetter(field: HTMLInputElement): void {
  const id = hintId(field);
  if (document.getElementById(id) === null) {
    const hint = document.createElement("small");
    hint.id = id;
    hint.className = "fhi-hint";
    hint.textContent = FROM_LETTER_HINT;
    if (field.parentNode !== null) {
      field.parentNode.insertBefore(hint, field.nextSibling);
    }
  }
  describedBy(field, id, true);
  follow(field);
}

function fieldValue(id: string): string {
  const field = document.getElementById(id) as HTMLInputElement | null;
  return field === null ? "" : field.value;
}

// Fills each About you field that is still empty from the letter, marks it,
// and keeps it the way typing would (`remember` is the page's storage
// helper, which honours "Remember what I typed"). Returns how many it
// filled. A storage write that fails (full storage) leaves the field
// filled and marked, and is not thrown on.
export function fillDetailsFromLetter(text: string, remember: (id: string, value: string) => void): number {
  const found = findDetailsInLetter(text);
  if (found.firstName === undefined || found.lastName === undefined) {
    return 0;
  }
  const typedFirst = fieldValue("store_fname");
  const typedLast = fieldValue("store_lname");
  const someoneElse =
    (typedFirst !== "" && !sameWords(typedFirst, found.firstName)) ||
    (typedLast !== "" && !sameWords(typedLast, found.lastName));
  if (someoneElse) {
    // The name already here is not the letter's: it is about someone else.
    return 0;
  }
  const typedStreet = fieldValue("store_street");
  const typedZip = fieldValue("store_zip");
  const zipAgrees = found.zip !== undefined && (typedZip === "" || typedZip.trim().slice(0, 5) === found.zip);
  // Whether the street field is closed to the fill: something is in it, the
  // person has typed in it or emptied it, or the page has no such field.
  const streetIsTheirs =
    typedStreet !== "" || leaveAlone.has("store_street") || document.getElementById("store_street") === null;
  const fillStreet = found.street !== undefined && zipAgrees && !streetIsTheirs;
  // The ZIP goes in only beside the letter's street (rule 4): the street
  // filled from the same block in this pass, the same street already in its
  // field, or, for a block with a unit line (no street to fill), a street
  // field still empty and not the person's. A street they emptied, or one
  // there with a unit line in the letter (no street to compare), keeps it
  // out.
  const fillZip =
    zipAgrees &&
    (fillStreet ||
      (found.street !== undefined && typedStreet !== "" && sameWords(typedStreet, found.street)) ||
      (found.street === undefined && !streetIsTheirs));
  let filled = 0;
  for (const [key, id] of FIELDS) {
    const value = found[key];
    if (value === undefined || (key === "street" && !fillStreet) || (key === "zip" && !fillZip)) {
      continue;
    }
    const field = document.getElementById(id) as HTMLInputElement | null;
    if (field === null || field.value !== "" || leaveAlone.has(id)) {
      continue;
    }
    field.value = value;
    leaveAlone.add(id);
    markFromLetter(field);
    filled += 1;
    try {
      remember(id, value);
    } catch {
      // Not remembered, as a typed value would not be either; the field
      // keeps what was filled and its hint.
    }
  }
  if (filled > 0) {
    updateNote();
  }
  return filled;
}

// Runs the fill when a letter arrives in the box by a paste, and once on
// load when the server put the letter there (its own reading of an upload,
// a treatment guide's opening line, a page sent back). Not on typing. A
// file read on this device calls fillDetailsFromLetter itself when the
// reading is done (scrub.ts). Called once the page has put back what this
// browser kept, so a field holding something now is the person's.
export function watchLetterForDetails(
  box: HTMLTextAreaElement,
  remember: (id: string, value: string) => void,
): void {
  for (const [, id] of FIELDS) {
    const field = document.getElementById(id) as HTMLInputElement | null;
    if (field !== null) {
      if (field.value !== "") {
        leaveAlone.add(id);
      }
      follow(field);
    }
  }
  box.addEventListener("paste", () => {
    // The pasted text is in the box only once this event is over.
    setTimeout(() => {
      fillDetailsFromLetter(box.value, remember);
    }, 0);
  });
  if (box.defaultValue.trim() !== "") {
    fillDetailsFromLetter(box.value, remember);
  }
}
