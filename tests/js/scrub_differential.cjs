'use strict';
// Run main's scrubbers and this branch's over the same generated letters and
// report every place this branch differs from main in a way the rule does
// not allow. Driven by tests/sync/test_scrub_never_leaves_what_main_removed.py.
//
//   node scrub_differential.cjs '<spec json>'
//
// spec:
//   main     directory with main's scrubbers compiled: scrub_scrub.js,
//            user_info_storage.js, typed_value_pattern.js (from
//            tests/js/scrub_before_whole_words, which are main's files at
//            1a24d58303bcce61314e52fb075b1b2ade6d2c8e, byte for byte)
//   branch   the same for this branch, or "main" (main against itself) or
//            "identity" (a scrubber that takes nothing out), which check
//            that the comparison itself can pass and can fail
//   inputs   [{id, type, value}, ...]: every input of the intake page, with
//            the value the page gives it
//   count    how many generated cases per seed, after the hand-written ones
//   seeds    for the generator, so every run makes the same letters
//
// Each case is what was typed (first and last name, email, street, ZIP and,
// for the chat, city) and a letter. Both run through Remove personal
// details (scrub_scrub.ts clean, on a page with the intake page's inputs)
// and the chat's scrubPersonalInfo (user_info_storage.ts).
//
// Words, as typed_value_pattern.ts has them, written again here rather than
// imported so the check does not share a mistake with what it checks: a
// word character is a letter of a script with capitals, a decimal digit, or
// a mark on one of those; a word is a run of them; there is a word edge
// between two characters unless both are word characters. A letter of a
// script without capitals (Chinese, Korean, Arabic, ...) is not a word
// character. A value has more than one word where it has more than one run
// of word characters and letters of a script without capitals all told.
//
// What is checked, per case and per scrubber:
//
// 1. Nothing main took out is left, but for a match that lies strictly
//    inside one word. Every String.prototype.replace the scrubbers make on
//    the text is recorded, so each character of the output is known to be a
//    character of the letter or one put in. For each match main replaced,
//    every letter, digit or mark of the letter it took out must be gone
//    from the branch's output too, unless the letters, digits and marks of
//    the match all lie in one word of the letter that goes on past them:
//    the "ann" of "annual", the "a." of "Mesa.", a value of one word left
//    inside a longer one. A match with a word edge inside it ("283 24th St"
//    in "283 24th Street", "Smith-Jones" in "Smith-Joneses") or with a
//    letter of a script without capitals in it is held to it in full. That
//    covers main's label rules too ("Dear Bob Roe" with nothing typed).
//    Matches from a value nobody typed (main also took the tick boxes'
//    "checked" out) are not held to this.
// 2. Nothing main kept is taken out, but the rest of a word a value of more
//    words runs on into. Each character of the letter main kept and the
//    branch took out must lie inside a match of a value the scrubber looks
//    for, found anywhere the way main found it in the text the values are
//    taken out of (for Remove personal details, the letter after the label
//    rules), that is not strictly inside one word as in 1 (main's chat
//    found names only with \b, which knew only A to Z, and main's Remove
//    personal details lost a value that an earlier one had broken up, so
//    this is main's matching rather than what main's chat took out); or it
//    must be the rest of the word that such a match of a value of more than
//    one word starts or ends inside, between the match and that word's own
//    end, and nothing past it. And of every word of that text the branch's
//    typed values took a character out of, they took out the whole word.
// 3. No placeholder of the site is written into. The branch never replaces
//    a character of a site placeholder ({{UPPER_CASE}} and the mixed-case
//    ones the code writes, listed in SITE_PLACEHOLDER), one already in the
//    letter or one it put in, nor puts text inside one; what its typed
//    values put in is site placeholders only; and every brace it put in is
//    part of one. Other text in double braces is text like the rest and is
//    held to 1 and 2.
//
// Writes one JSON object to stdout: the counts, and for each kind of
// violation how many there were and the first few.

const path = require('path');
const Module = require('module');
const {buildPage, install} = require(path.join(__dirname, 'fake_page.cjs'));

const [, , specJson] = process.argv;
if (!specJson) {
  throw new Error("usage: scrub_differential.cjs '<spec json>'");
}
const spec = JSON.parse(specJson);

for (const level of ['debug', 'log', 'info', 'warn', 'error', 'trace', 'dir', 'table']) {
  console[level] = () => {};
}

// ---------------------------------------------------------------- the page

const page = buildPage('<form id="fuck_health_insurance_form"><textarea id="denial_text"></textarea></form>');
install(page);
const doc = page.document;
const form = doc.getElementById('fuck_health_insurance_form');
const pageInputs = spec.inputs.map(({id, type, value}) => {
  const input = doc.createElement('input');
  if (id) input.id = id;
  if (type) input.type = type;
  form.appendChild(input);
  return {input, value: value || ''};
});
const TYPED_IDS = ['store_fname', 'store_lname', 'email', 'store_street', 'store_zip'];
for (const id of TYPED_IDS) {
  if (!pageInputs.some(({input}) => input.id === id)) {
    throw new Error('the intake page has no input ' + id);
  }
}
const box = doc.getElementById('denial_text');

// shared.js loads pdf.js, which this never reaches.
const load = Module._load;
Module._load = function (request, ...rest) {
  if (request === 'pdfjs-dist') return {GlobalWorkerOptions: {}};
  return load.call(this, request, ...rest);
};

function scrubbersIn(dir) {
  return {
    clean: require(path.join(dir, 'scrub_scrub.js')).clean,
    chat: require(path.join(dir, 'user_info_storage.js')).scrubPersonalInfo,
  };
}
const main = scrubbersIn(spec.main);
// Main's label rules ("Dear", "Patient:", ...), read out of its compiled
// scrubber: each is new RegExp("<source>", "gmi").
const MAIN_LABEL_SOURCES = new Set();
{
  const compiled = require('fs').readFileSync(path.join(spec.main, 'scrub_scrub.js'), 'utf8');
  const rule = /new RegExp\(("(?:[^"\\]|\\.)*"), "gmi"\)/g;
  let m;
  while ((m = rule.exec(compiled)) !== null) {
    // A JavaScript string literal from main's own file, not JSON: one of
    // them has a \. in it.
    MAIN_LABEL_SOURCES.add(new RegExp(new Function('return ' + m[1])(), 'gmi').source);
  }
  if (MAIN_LABEL_SOURCES.size < 10) {
    throw new Error("could not read main's label rules: " + MAIN_LABEL_SOURCES.size);
  }
}
const mainPattern = require(path.join(spec.main, 'typed_value_pattern.js')).typedValuePattern;
const branch =
  spec.branch === 'main' ? main : spec.branch === 'identity' ? null : scrubbersIn(spec.branch);

// ------------------------------------------------------- following the text

// The placeholders the site writes: {{UPPER_CASE}}, and the mixed-case ones
// the scrubbers, the letters and the replies use.
const SITE_PLACEHOLDER = new RegExp(
  '\\{\\{(?:[A-Z][A-Z0-9_ ]*|Your Name|Your Email Address|Your Phone Number|Your Address|' +
    'date|today|insurance_company|patient_name|patient_dob|provider_name|provider_npi|' +
    'practice_name|practice_address)\\}\\}',
  'g',
);
const ONLY_SITE_PLACEHOLDERS = new RegExp(
  '^(?:' + SITE_PLACEHOLDER.source + '(?: ' + SITE_PLACEHOLDER.source + ')*)?$',
);

// Where each character of the text came from: its index in the letter, or
// -1 for one a replacement put in; and which site placeholder it is part
// of, 0 for none.
let tracker = null;
let nextPlaceholderId = 1;

function placeholderIds(text) {
  const ids = new Array(text.length).fill(0);
  const re = new RegExp(SITE_PLACEHOLDER.source, 'g');
  let m;
  while ((m = re.exec(text)) !== null) {
    const id = nextPlaceholderId++;
    for (let k = m.index; k < m.index + m[0].length; k++) ids[k] = id;
  }
  return ids;
}

function startTracking(letter) {
  tracker = {
    letter,
    text: letter,
    origin: Array.from({length: letter.length}, (_, k) => k),
    placeholder: placeholderIds(letter),
    steps: [],
    busy: false,
  };
}

function stopTracking(output) {
  const t = tracker;
  tracker = null;
  if (t.text !== output) {
    throw new Error('lost track of the text: ' + JSON.stringify({letter: t.letter, output, tracked: t.text}));
  }
  return t;
}

function step(str, pattern, records, result) {
  const t = tracker;
  const origin = [];
  const placeholder = [];
  const changes = [];
  let pos = 0;
  for (const r of records) {
    for (let k = pos; k < r.offset; k++) {
      origin.push(t.origin[k]);
      placeholder.push(t.placeholder[k]);
    }
    const m = r.matched;
    const rep = r.rep;
    let p = 0;
    while (p < m.length && p < rep.length && m[p] === rep[p]) p++;
    let s = 0;
    while (s < m.length - p && s < rep.length - p && m[m.length - 1 - s] === rep[rep.length - 1 - s]) s++;
    for (let k = 0; k < p; k++) {
      origin.push(t.origin[r.offset + k]);
      placeholder.push(t.placeholder[r.offset + k]);
    }
    const removed = [];
    let touches = false;
    for (let k = r.offset + p; k < r.offset + m.length - s; k++) {
      if (t.origin[k] >= 0) removed.push(t.origin[k]);
      if (t.placeholder[k] !== 0) touches = true;
    }
    const inserted = rep.slice(p, rep.length - s);
    const left = r.offset + p - 1;
    const right = r.offset + m.length - s;
    if (
      inserted !== '' &&
      left >= 0 &&
      right < str.length &&
      t.placeholder[left] !== 0 &&
      t.placeholder[left] === t.placeholder[right]
    ) {
      touches = true;
    }
    const insertedIds = placeholderIds(rep);
    for (let k = p; k < rep.length - s; k++) {
      origin.push(-1);
      placeholder.push(insertedIds[k]);
    }
    for (let k = m.length - s; k < m.length; k++) {
      origin.push(t.origin[r.offset + k]);
      placeholder.push(t.placeholder[r.offset + k]);
    }
    pos = r.offset + m.length;
    if (rep !== m) {
      changes.push({
        offset: r.offset,
        matched: m,
        rep,
        removed,
        // What it took out of the text it was given, as [start, end).
        at: [r.offset + p, r.offset + m.length - s],
        touches,
        firstOrigin: t.origin[r.offset],
        lastOrigin: t.origin[r.offset + m.length - 1],
      });
    }
  }
  for (let k = pos; k < str.length; k++) {
    origin.push(t.origin[k]);
    placeholder.push(t.placeholder[k]);
  }
  if (origin.length !== result.length) {
    throw new Error('replacement bookkeeping is off');
  }
  t.steps.push({
    source: pattern instanceof RegExp ? pattern.source : String(pattern),
    flags: pattern instanceof RegExp ? pattern.flags : '',
    before: str,
    originBefore: t.origin,
    changes,
  });
  t.text = result;
  t.origin = origin;
  t.placeholder = placeholder;
}

const nativeReplace = String.prototype.replace;
String.prototype.replace = function (pattern, replacement) {
  const str = String(this);
  // A replacement with a $ pattern in it is a helper escaping a value: the
  // scrubbers' placeholders have none. Were one to, the text would no longer
  // match what is tracked, which stopTracking reports.
  if (
    tracker === null ||
    tracker.busy ||
    str !== tracker.text ||
    (typeof replacement !== 'function' && String(replacement).indexOf('$') >= 0)
  ) {
    return nativeReplace.call(this, pattern, replacement);
  }
  tracker.busy = true;
  const records = [];
  let result;
  try {
    result = nativeReplace.call(str, pattern, function (...args) {
      const matched = args[0];
      const last = args[args.length - 1];
      const at = typeof last === 'string' ? args.length - 2 : args.length - 3;
      const rep =
        typeof replacement === 'function' ? String(replacement.apply(undefined, args)) : String(replacement);
      records.push({offset: args[at], matched, rep});
      return rep;
    });
  } finally {
    tracker.busy = false;
  }
  // A scrubber's replace always puts a placeholder in. One that does not is
  // a helper turning a typed value that happens to equal the whole text
  // into a pattern, and the text itself is not touched.
  if (result !== str && records.some((r) => r.rep !== r.matched && r.rep.indexOf('{{') >= 0)) {
    step(str, pattern, records, result);
  }
  return result;
};

// -------------------------------------------------------------- words

const CASED_LETTER = /^[\p{Lu}\p{Ll}\p{Lt}]$/u;
const DECIMAL_DIGIT = /^\p{Nd}$/u;
const LETTER = /^\p{L}$/u;
const MARK = /^\p{M}$/u;
const LETTER_DIGIT_OR_MARK = /^[\p{L}\p{N}\p{M}]$/u;

function codePointAt(text, i) {
  return String.fromCodePoint(text.codePointAt(i));
}

// A letter of a script with capitals, or a decimal digit.
function casedOrDigit(ch) {
  return (
    CASED_LETTER.test(ch) ||
    DECIMAL_DIGIT.test(ch) ||
    (LETTER.test(ch) && ch.toUpperCase() !== ch.toLowerCase())
  );
}

// The words of a text, read from the start: for each code unit, the index
// of the word it is part of or -1, and each word as {start, end}. A mark
// is part of the word its letter or digit is.
function wordMap(text) {
  const index = new Int32Array(text.length).fill(-1);
  const words = [];
  let baseIsWord = false;
  let current = null;
  for (let i = 0; i < text.length; ) {
    const ch = codePointAt(text, i);
    const mark = MARK.test(ch);
    const isWord = mark ? baseIsWord : casedOrDigit(ch);
    if (!mark) baseIsWord = isWord;
    if (isWord) {
      if (current === null) {
        current = {start: i, end: i};
        words.push(current);
      }
      current.end = i + ch.length;
      for (let k = i; k < i + ch.length; k++) index[k] = words.length - 1;
    } else {
      current = null;
    }
    i += ch.length;
  }
  return {index, words};
}

function edgeAt(map, i) {
  return !(i > 0 && i < map.index.length && map.index[i - 1] >= 0 && map.index[i] >= 0);
}

// Letters, digits and marks, per code unit.
function lettersDigitsAndMarks(text) {
  const flags = new Uint8Array(text.length);
  for (let i = 0; i < text.length; ) {
    const ch = codePointAt(text, i);
    if (LETTER_DIGIT_OR_MARK.test(ch)) for (let k = i; k < i + ch.length; k++) flags[k] = 1;
    i += ch.length;
  }
  return flags;
}

// More than one run of word characters and letters of a script without
// capitals, all told.
function ofSeveralWords(value) {
  let words = wordMap(value).words.length;
  for (let i = 0; i < value.length; ) {
    const ch = codePointAt(value, i);
    if (LETTER.test(ch) && !casedOrDigit(ch)) words++;
    i += ch.length;
  }
  return words > 1;
}

// Whether these positions of the letter (its letters, digits and marks in
// one match) all lie in one word that goes on past them.
function strictlyInsideOneWord(map, positions) {
  if (positions.length === 0) return false;
  const w = map.index[positions[0]];
  if (w < 0) return false;
  let lo = Infinity;
  let hi = -Infinity;
  for (const p of positions) {
    if (map.index[p] !== w) return false;
    lo = Math.min(lo, p);
    hi = Math.max(hi, p + 1);
  }
  const word = map.words[w];
  return word.start < lo || word.end > hi;
}

function escapeRegExp(text) {
  return nativeReplace.call(text, /[.*+?^${}()|[\]\\]/g, '\\$&');
}

// --------------------------------------------------------------- the corpus

function mulberry32(seed) {
  let a = seed >>> 0;
  return function () {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const FIRST = [
  'Ann', 'Ed', 'Al', 'Sam', 'Jo', 'A', 'M', 'I', 'Chris', 'Christopher', 'Annabelle',
  'José', 'Zoë', 'Renée', 'Mary Ann', 'Mary-Ann', 'Jean-Luc', "D'Andre", 'J.', 'Name',
  'Joe', 'Will', 'Hope', 'Day', 'Same', '小明', '伟', '太郎', '민수', 'Мария', 'Ελένη',
  'محمد', ' Ann ', 'ANN', 'ann', 'Lee', 'Ma', 'Patient', 'Dear', 'Ng', 'Ann.', 'Dr. Ann',
  'Ann Ann', 'Anna', 'Lee Ann', 'Ann王', 'Mary Ann Lee', 'A.', 'M.', 'علي', '김민수', '지은',
  'أحمد', 'فاطمة',
];
const LAST = [
  'Doe', 'Day', 'Smith', 'Smith-Jones', "O'Neill", 'O’Brien', 'de la Cruz', 'Núñez',
  'van der Berg', '王', '田中', '김', 'Name', 'Price', 'Patient', 'Doering', 'Lee', 'Li',
  'Ng', 'Group', 'Member', 'Ann', 'Same', 'Jones Jr.', "D'Angelo-Ruiz", 'Müller',
  'MacDonald', 'St. John', 'Doe,', 'X', 'Lee Ann', 'Smith Smith', 'Ann Lee', 'حسن', '박',
];
const EMAIL = [
  'ann@example.com', 'ann.doe@example.com', 'asmith@example.org', 'j.doe+fhi@mail.example.net',
  'ann_doe@example.com', 'sam@day.example', 'min.su@example.kr', 'jose.nunez@example.com',
  'x@y.z', 'ANN@EXAMPLE.COM', 'ali@example.com',
];
const STREET = [
  '123 Main St', '123 Main St , Apt 4B', '123 Main St, Apt 4B', '123 Sample Street Apt 4B',
  '283 24th St', '9 Oak Ave.', '#4B 12 Elm Rd', '123 王府井大街', "1 Rue de l'Église",
  '500 N. State St', 'PO Box 12', '12 Day St', '4 Sam Ct', '77 Ann St.', '10 Downing St',
  '1600 Pennsylvania Ave NW', '123 Main St. ,  Apt. 4B', '5 Ave . B', '1 José St',
  '10 Main St', '9 Lee Ann Ct', '62701 Ann St', '283 24th St.', '12 Ann Way',
];
const ZIP = ['62701', '62701-1234', '62701 1234', '94103', 'SW1A 1AA', '02134', '1234', '627011234', '62701\t1234'];
const CITY = [
  'Springfield', 'Mesa', 'Day', 'Saint-Denis', 'São Paulo', '北京', 'Same', 'Ann Arbor',
  'St Louis', 'San José', 'Lee', 'San Francisco',
];
// Names in the letter that nobody typed, where a typed value is empty.
const UNTYPED = ['Bob', 'Roe', 'Kim', 'Pat Roe', 'Ana', 'Lu'];

const MEDICAL = [
  'Your claim for type 2 diabetes was denied.',
  'Same day services require prior authorization.',
  'Your annual limit was reached.',
  'Not medically necessary.',
  'Medicare Part B covers a plan.',
  'I asked for a review.',
  'The same price applies.',
  '患者患有2型糖尿病。',
  'We checked the records.',
  'Example Health Plan',
  'Edward reviewed the denied claim.',
  "Annette Doering's notes",
  'Mesalamine was denied.',
  'Christopher Roe, MD',
  "don't wait",
  'the inpatient stay and outpatient services',
  'Smith-Jones Clinic',
  "O'Neill Hospital",
  'Day surgery',
  'Name of plan: Acme',
  'Hope Medical Group',
  '王国医院',
  'a 30-day supply',
  'Policy 62701-A',
  'claim 627012',
  '1123 Main Street',
  'has type 2 diabetes',
  'Physical therapy is covered at 80%.',
  'Group: Acme Employees',
  'Lee Memorial',
  'This is a denial of a claim... Part A. You have a right',
  'Medicare Part A. covers a.m. visits at Mesa.',
  'السلام عليكم، توكلت على الله وعليه',
  'تم رفض طلبكم للعلاج.',
  '김민수님께 보험 청구가 거부되었습니다.',
  'Joann Doe and Annette Doering',
  '283 24th Street',
];
const PLACEHOLDER_LINES = [
  'Dear {{FIRST_NAME}} {{LAST_NAME}},',
  '{{PATIENT_NAME}} applied.',
  'Write to {{Your Email Address}}.',
  '{{ADDRESS}}\n{{CITY}}, IL {{ZIP_CODE}}',
  'Patient: {{FIRST_NAME}} {{LAST_NAME}}',
  'Subscriber ID: {{SCSID}}',
  'Group ID: {{GPID}}',
  "{{PATIENT_NAME}}'s claim",
  'Call {{Your Phone Number}}.',
  'Sincerely, {{Your Name}}',
];

function generator(seed) {
  const rng = mulberry32(seed);
  const pick = (list) => list[Math.floor(rng() * list.length)];
  const chance = (p) => rng() < p;

  // A value whose first word runs on to the left or whose last word runs on
  // to the right, as a letter might print it longer.
  const runOn = (v) =>
    chance(0.5)
      ? pick(['Jo', '1', '2', 'Rose', 'x', 'Mc']) + v
      : v + pick(['reet', 'ette', 'ering', 's', 'x', 'eet', '1']);

  const transforms = [
    (v) => v.toUpperCase(),
    (v) => v.toLowerCase(),
    (v) => nativeReplace.call(v, /\S+/g, (w) => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase()),
    (v) => nativeReplace.call(v, / /g, () => pick([' ', '  ', '\n', '\t', '\u00a0', ' \n'])),
    (v) => nativeReplace.call(v, /['’]/g, (c) => (c === "'" ? '’' : "'")),
    (v) => v + pick(["'s", "'S", '’s', '’S', "'"]),
    (v) => (chance(0.5) ? pick(['Jo', 'x', 'Mc', '1', 'É', 'é']) + v : v + pick(['ette', 's', 'y', '1', 'ing', 'ual', 'é', '́'])),
    runOn,
    (v) => {
      const [a, b] = pick([['(', ')'], ['"', '"'], ['“', '”'], ['-', '-'], ['_', '_'], ['<', '>'], ['[', ']'], ['#', ''], ['', ','], ['', '.'], ['', ';'], ['', ':'], ['/', '/'], ['{{', '}}']]);
      return a + v + b;
    },
    (v) => {
      const [a, b] = pick([
        ['患者', '的申请'], ['我的电子邮箱是', '。'], ['邮编', '号'], ['', '患有2型糖尿病。'],
        ['김', '님께'], ['', '님'], ['', '씨'], ['', '의'], ['', '에게'], ['เรียนคุณ', ''],
        ['و', ''], ['ل', ''], ['ب', ''], ['وال', ''], ['', 'كم'], ['', 'ه'],
        ['السيد', 'في'], ['بريدي', '،'], ['إلى', ''], ['', 'الذي'],
      ]);
      return a + v + b;
    },
    (v) => {
      const [a, b] = pick([['first.', ''], ['', '.au'], ['', '.com'], ['', '.'], ['', '@x'], ['mailto:', '']]);
      return a + v + b;
    },
    (v) => v + pick(['-1234', ' 1234', '  1234', '\t1234', '\u00a01234', '1234', '\n1234']),
    (v) => nativeReplace.call(v, /,/g, () => pick([' ,', ', ', ' , ', ',', '  ,  '])),
    (v) => nativeReplace.call(v, /\./g, () => pick(['.', '', ' .'])),
  ];

  // A value as a letter might print it.
  const render = (v) => {
    if (chance(0.45)) return v;
    let out = pick(transforms)(v);
    if (chance(0.3)) out = pick(transforms)(out);
    return out;
  };

  const typedSet = () => {
    const t = {
      fname: chance(0.92) ? pick(FIRST) : '',
      lname: chance(0.92) ? pick(LAST) : '',
      street: chance(0.8) ? pick(STREET) : '',
      zip: chance(0.85) ? pick(ZIP) : '',
      city: chance(0.7) ? pick(CITY) : '',
    };
    const latin = (s) => /^[A-Za-z' .-]+$/.test(s);
    if (chance(0.3) && t.fname && t.lname && latin(t.fname) && latin(t.lname)) {
      const part = (s) => nativeReplace.call(s.toLowerCase(), /[^a-z]/g, '');
      t.email = part(t.fname) + pick(['.', '', '_']) + part(t.lname) + '@example.com';
    } else {
      t.email = chance(0.8) ? pick(EMAIL) : '';
    }
    return t;
  };

  const fragments = [
    (t) => 'Dear ' + t.R('fname') + ' ' + t.R('lname') + pick([',', ':', '', '\n']),
    (t) => 'Dear ' + t.R('fname') + pick([',', '', '!']),
    (t) => 'Dear ' + pick(['Mr. ', 'Ms. ', '']) + t.R('lname') + ',',
    (t) => 'Patient: ' + t.R('fname') + ' ' + t.R('lname') + pick(['', ' ', '\n']) + pick(MEDICAL),
    (t) => pick(['Patient: ', 'Patient ', 'patient: ', 'PATIENT: ']) + t.R('lname') + t.R('fname') + pick(['患有2型糖尿病。', 'has type 2 diabetes.', '']),
    (t) => 'Member: ' + t.R('lname') + pick([', ', ' ']) + t.R('fname'),
    (t) => pick(['Your inpatient ', 'outpatient ', 'endear ', 'subgroup: ', 'impatient ', 'Dearborn ', '_patient: ']) + t.R('fname') + pick([' stay', '', '.']),
    (t) => t.R('fname') + pick([' ', '\n', '  ', '\t', '\u00a0', '']) + t.R('lname'),
    (t) => t.R('lname') + pick([', ', ',', ' ', '\n']) + t.R('fname'),
    (t) => t.R('street') + pick(['\n', ', ', ' ', '']) + t.city() + pick([', IL ', ' IL ', '\n', ' ']) + t.R('zip'),
    (t) => pick(['Write to ', 'Email: ', '我的电子邮箱是', '', '<', 'mailto:', 'بريدي الإلكتروني ', 'بريدي']) + t.R('email') + pick(['.', '', '。', '>', '.au', ',', 'في', '،']),
    (t) => pick(['IL ', 'ZIP: ', '邮编', 'Springfield, IL ']) + t.R('zip') + pick(['', '-1234', ' 1234', '\u00a01234']),
    () => pick(MEDICAL),
    () => pick(MEDICAL),
    () => pick(PLACEHOLDER_LINES),
    (t) => pick(['{{FIRST_NAME}}', '{{PATIENT_NAME}} ', '{{LAST_NAME}}, ']) + t.R(pick(['fname', 'lname'])),
    (t) => t.R(pick(['fname', 'lname'])) + pick(['{{LAST_NAME}}', ' {{FIRST_NAME}}', '{{Your Email Address}}']),
    () => pick(['Subscriber ID: XYZ000000', 'Group number: G12345', 'Group ID: 55555']),
    (t) => t.R(pick(['fname', 'lname', 'email', 'street', 'zip', 'city'])),
    (t) => '患者' + t.R('fname') + pick([' ', '']) + t.R('lname') + '的申请',
    (t) => t.R('lname') + t.R('fname') + pick(['様', '님께', '的申请', '', '님', '씨', '의', '에게']),
    // A value of more words printed longer at either end.
    (t) => pick(['', 'I live at ', 'Address: ']) + t.Run('street') + pick(['\n', ', ', '.']) + t.city(),
    (t) => pick(['', 'Dear ', 'Patient: ']) + (chance(0.5) ? pick(['Jo', 'Rose', 'x']) : '') + t.R('fname') + ' ' + t.R('lname') + (chance(0.5) ? pick(['ering', 's', 'ette']) : ''),
    (t) => pick(['Write to ', '', 'Email: ']) + t.Run('email') + pick(['.', '', ' or']),
    (t) => t.Run(pick(['fname', 'lname', 'zip'])),
    // Text in double braces that is not a placeholder of the site.
    (t) => '{{' + pick(['', 'Ref ', 'Dear ', 'Patient ']) + t.R('fname') + pick([' ' + t.R('lname'), '', ', ' + t.R('email')]) + '}}',
    (t) => '{{' + t.R(pick(['email', 'street', 'zip', 'lname'])) + '}}',
    // A name inside Arabic words, with a prefix joined to it, and a Korean
    // name with a particle; an initial with its period.
    (t) => 'السلام ' + t.R('fname') + 'كم، توكلت على الله و' + t.R('fname') + 'ه',
    (t) => pick(['إلى ', 'شكرا ', '']) + pick(['و', 'ل', 'ب', 'وال']) + t.R(pick(['fname', 'lname'])) + pick(['', ' ', '،']),
    (t) => t.R(pick(['fname', 'lname'])) + pick(['님께', '님', '씨에게', '의']) + ' 보험 청구가 거부되었습니다.',
    (t) => 'Part ' + t.R('fname') + ' You have a right to ' + pick(['a', 'A', 'an']) + ' review.',
    // A Latin name or an email right against Arabic text.
    (t) => pick(['السيد', 'إلى', 'بريدي', 'المريض']) + t.R(pick(['fname', 'email', 'lname'])) + pick(['في', '،', 'الذي', '']),
  ];
  const separators = ['\n', ' ', '\n\n', '', '. ', '。', '\t', ', '];

  return function next() {
    const typed = typedSet();
    const ctx = {
      R: (field) => (typed[field] ? render(typed[field]) : pick(UNTYPED)),
      Run: (field) => (typed[field] ? runOn(typed[field]) : pick(UNTYPED)),
      city: () => (typed.city ? render(typed.city) : pick(['Springfield', 'Anytown'])),
    };
    const parts = [];
    const n = 1 + Math.floor(rng() * 6);
    for (let i = 0; i < n; i++) {
      if (i > 0) parts.push(pick(separators));
      parts.push(pick(fragments)(ctx));
    }
    return {typed, letter: parts.join('')};
  };
}

// The cases the reviews found, and the gaps they listed, first.
const HAND_WRITTEN = [
  [{fname: 'Ann', lname: 'Doe'}, '患者Ann Doe的申请'],
  [{email: 'ann@example.com'}, '我的电子邮箱是ann@example.com。'],
  [{street: '123 Main St , Apt 4B'}, '123 Main St , Apt 4B'],
  [{fname: 'José', lname: "O'Neill"}, "Dear José O'Neill"],
  [{fname: 'José', lname: 'Smith-Jones'}, 'Dear José Smith-Jones'],
  [{fname: 'José', lname: 'de la Cruz'}, 'Dear José de la Cruz'],
  [{fname: '小明', lname: '王'}, 'Patient: 王小明患有2型糖尿病。'],
  [{zip: '62701 1234'}, '62701  1234'],
  [{zip: '62701 1234'}, '62701\t1234'],
  [{zip: '62701 1234'}, '62701\u00a01234'],
  [{fname: 'Chris', lname: 'Doe'}, "CHRISTOPHER DOE'S appeal"],
  [{fname: 'Sam', lname: 'Day'}, 'Same day services require prior authorization.'],
  [{fname: 'Joe', lname: 'Name'}, 'Joe Name applied.'],
  [{fname: 'A', lname: 'Smith'}, 'A Smith'],
  [{fname: 'Chris', lname: 'Doe'}, 'Christopher J. Doe'],
  [{fname: 'Chris', lname: 'Doe'}, 'DOE CHRISTOPHER'],
  [{email: 'ann@example.com'}, 'first.ann@example.com'],
  [{email: 'ann@example.com'}, 'ann@example.com.au'],
  [{fname: 'Ann', lname: 'Doe'}, 'Ann Doe applied. {{PATIENT_NAME}} and {{FIRST_NAME}} {{LAST_NAME}}'],
  [{fname: 'Ann', lname: 'Doe'}, 'Your inpatient stay; outpatient Ann Doe; _patient: Bob'],
  [{fname: 'Ann'}, 'Joann Ann ann ANN Ann\u0301 éAnn Ann_Doe'],
  [{street: '123 王府井大街'}, '123 王府井大街123 王府井大街'],
  // The second review's.
  [{fname: 'Ann', lname: 'Doe', street: '283 24th St', zip: '94103'}, 'Ann Doe\n283 24th Street\nSan Francisco, CA 94103'],
  [{street: '283 24th St'}, '283 24th Street'],
  [{fname: 'A.'}, 'This is a denial of a claim... Part A. You have a right'],
  [{fname: 'علي'}, 'السلام عليكم، توكلت على الله وعليه'],
  [{fname: 'Ann', lname: 'Doe'}, 'Ref {{Ann Doe}}'],
  [{fname: 'Ann', lname: 'Doe'}, 'Joann Doe, Annette Doe, Ann Doering'],
  [{fname: '민수', lname: '김'}, '김민수님께, 김민수씨'],
  [{fname: 'علي'}, 'إلى وعلي ولعلي'],
  [{fname: 'Ann', email: 'ann@example.com'}, 'بريديann@example.comفي السيدAnnالذي'],
  [{email: 'ann.doe@example.com'}, 'jann.doe@example.com ann.doe@example.comx'],
  [{lname: 'Smith-Jones'}, 'Smith-Joneses'],
];

// ------------------------------------------------------------ one scrubber

// What Remove personal details looks for: each About you box's value, and
// it run together with every typed box (the tick boxes' values left out),
// and the email box's value. Main looked for these too.
function valuesOnTheForm(typed) {
  const stores = [typed.fname, typed.lname, typed.street, typed.zip].filter((v) => v);
  const all = stores.concat(typed.email ? [typed.email] : []);
  const values = all.slice();
  for (const v of stores) for (const w of all) values.push(v + w);
  return values;
}

// What the chat looks for.
function valuesInTheChat(typed) {
  const values = [typed.email];
  if (typed.fname && typed.lname) values.push(typed.fname + ' ' + typed.lname);
  values.push(typed.fname, typed.lname, typed.street, typed.city, typed.zip);
  return values.filter((v) => v);
}

function typedSourcesForTheForm(typed) {
  const sources = new Set();
  for (const value of valuesOnTheForm(typed)) {
    const pattern = mainPattern(value);
    if (pattern !== null) sources.add(new RegExp(pattern, 'gi').source);
  }
  return sources;
}

function runForm(scrubbers, typed, letter) {
  const values = {
    store_fname: typed.fname || '',
    store_lname: typed.lname || '',
    email: typed.email || '',
    store_street: typed.street || '',
    store_zip: typed.zip || '',
  };
  for (const {input, value} of pageInputs) {
    input.value = Object.prototype.hasOwnProperty.call(values, input.id) ? values[input.id] : value;
  }
  box.value = letter;
  startTracking(letter);
  try {
    scrubbers.clean();
  } catch (e) {
    tracker = null;
    throw e;
  }
  return stopTracking(box.value);
}

function userInfoFor(typed) {
  return {
    firstName: typed.fname || '',
    lastName: typed.lname || '',
    email: typed.email || '',
    address: typed.street || '',
    city: typed.city || '',
    state: 'IL',
    zipCode: typed.zip || '',
    acceptedTerms: true,
  };
}

function runChat(scrubbers, typed, letter) {
  startTracking(letter);
  let out;
  try {
    out = scrubbers.chat(letter, userInfoFor(typed));
  } catch (e) {
    tracker = null;
    throw e;
  }
  return stopTracking(out);
}

function untouched(letter) {
  return {
    letter,
    text: letter,
    origin: Array.from({length: letter.length}, (_, k) => k),
    placeholder: placeholderIds(letter),
    steps: [],
  };
}

// --------------------------------------------------------------- the checks

// Examples kept of each kind of violation.
const EXAMPLES = 5;
const report = {
  cases: 0,
  handWritten: HAND_WRITTEN.length,
  seeds: spec.seeds,
  kinds: {},
  kindsBySeed: {},
  form: newCounts(),
  chat: newCounts(),
};

function newCounts() {
  return {
    mainMatches: 0,
    mainMatchesInsideAWord: 0,
    mainMatchesFromUntypedBoxes: 0,
    charactersChecked: 0,
    charactersMainKeptChecked: 0,
    // Characters main kept that the branch took out as the rest of a word
    // a value of more words runs on into.
    runOnCharactersTakenOut: 0,
    wordsTakenOutByTypedValues: 0,
    // {kind: {count, examples}}
    violations: {},
  };
}

function violation(counts, kind, details) {
  const found = (counts.violations[kind] = counts.violations[kind] || {count: 0, examples: []});
  found.count++;
  if (found.examples.length < EXAMPLES) found.examples.push(details);
}

// typedValues: every value the scrubber looks for.
function compare(counts, which, typed, typedValues, letter, mainRun, branchRun, typedSources) {
  const mainKept = new Uint8Array(letter.length);
  for (const o of mainRun.origin) if (o >= 0) mainKept[o] = 1;
  const branchKept = new Uint8Array(letter.length);
  for (const o of branchRun.origin) if (o >= 0) branchKept[o] = 1;
  const inPlaceholder = placeholderIds(letter);
  const map = wordMap(letter);
  const held = lettersDigitsAndMarks(letter);
  const describe = () => ({scrubber: which, typed, letter, main: mainRun.text, branch: branchRun.text});

  // 1. Nothing main took out is left, but for a match strictly inside one
  // word.
  for (const s of mainRun.steps) {
    const label = MAIN_LABEL_SOURCES.has(s.source);
    const fromTyped = label || typedSources === null || typedSources.has(s.source);
    for (const c of s.changes) {
      const taken = c.removed.filter((o) => held[o] && !inPlaceholder[o]);
      if (taken.length === 0) continue;
      counts.mainMatches++;
      if (!fromTyped) {
        counts.mainMatchesFromUntypedBoxes++;
        continue;
      }
      if (strictlyInsideOneWord(map, taken)) {
        counts.mainMatchesInsideAWord++;
        continue;
      }
      counts.charactersChecked += taken.length;
      const left = taken.filter((o) => branchKept[o]);
      if (left.length) {
        violation(counts, 'left what main took out', Object.assign(describe(), {
          mainMatched: c.matched,
          left: left.map((o) => letter[o]).join(''),
        }));
      }
    }
  }

  // 2. Nothing main kept is taken out, but the rest of a word a value of
  // more words runs on into. The values are looked for, the way main looked
  // for them, in the text they were taken out of: for Remove personal
  // details, the letter after the label rules, where "Patients" may now be
  // "Patient:" and a whole word.
  const typedStep = branchRun.steps.find((s) => !MAIN_LABEL_SOURCES.has(s.source));
  const searched = typedStep ? typedStep.before : branchRun.text;
  const searchedOrigin = typedStep ? typedStep.originBefore : branchRun.origin;
  const searchedMap = wordMap(searched);
  const searchedHeld = lettersDigitsAndMarks(searched);
  const inAMatch = new Uint8Array(letter.length);
  const runOn = new Uint8Array(letter.length);
  const mark = (flags, from, to) => {
    for (let k = from; k < to; k++) if (searchedOrigin[k] >= 0) flags[searchedOrigin[k]] = 1;
  };
  for (const value of typedValues) {
    const source = mainPattern(value);
    if (source === null) continue;
    const re = new RegExp(source, 'gi');
    const several = ofSeveralWords(value);
    let m;
    while ((m = re.exec(searched)) !== null) {
      const start = m.index;
      const end = start + m[0].length;
      re.lastIndex = start + 1;
      const letters = [];
      for (let k = start; k < end; k++) if (searchedHeld[k]) letters.push(k);
      if (strictlyInsideOneWord(searchedMap, letters)) continue;
      mark(inAMatch, start, end);
      if (several && !edgeAt(searchedMap, start)) {
        mark(runOn, searchedMap.words[searchedMap.index[start]].start, start);
      }
      if (several && !edgeAt(searchedMap, end)) {
        mark(runOn, end, searchedMap.words[searchedMap.index[end]].end);
      }
    }
  }
  const tookOut = [];
  for (let k = 0; k < letter.length; k++) {
    if (!mainKept[k] || inPlaceholder[k]) continue;
    counts.charactersMainKeptChecked++;
    if (branchKept[k] || inAMatch[k]) continue;
    if (runOn[k]) {
      counts.runOnCharactersTakenOut++;
      continue;
    }
    tookOut.push(k);
  }
  if (tookOut.length) {
    violation(counts, 'took out text main kept', Object.assign(describe(), {
      tookOut: tookOut.map((o) => letter[o]).join(''),
    }));
  }
  // Read in the text the typed values were taken out of, which is the
  // letter after the label rules for Remove personal details.
  for (const s of branchRun.steps) {
    if (MAIN_LABEL_SOURCES.has(s.source)) continue;
    const gone = new Uint8Array(s.before.length);
    for (const c of s.changes) for (let k = c.at[0]; k < c.at[1]; k++) gone[k] = 1;
    for (const w of wordMap(s.before).words) {
      let touched = false;
      let whole = true;
      for (let k = w.start; k < w.end; k++) {
        if (gone[k]) touched = true;
        else whole = false;
      }
      if (!touched) continue;
      counts.wordsTakenOutByTypedValues++;
      if (!whole) {
        violation(counts, 'took out part of a word', Object.assign(describe(), {
          word: s.before.slice(w.start, w.end),
        }));
      }
    }
  }

  // 3. No placeholder of the site is written into.
  for (const s of branchRun.steps) {
    const label = MAIN_LABEL_SOURCES.has(s.source);
    for (const c of s.changes) {
      if (c.touches) {
        violation(counts, 'wrote into a placeholder', Object.assign(describe(), {matched: c.matched}));
      }
      if (!label && !ONLY_SITE_PLACEHOLDERS.test(c.rep)) {
        violation(counts, 'put in something other than a placeholder', Object.assign(describe(), {put: c.rep}));
      }
    }
  }
  const output = branchRun.text;
  const inOutputPlaceholder = placeholderIds(output);
  for (let k = 0; k < output.length; k++) {
    if ((output[k] === '{' || output[k] === '}') && branchRun.origin[k] < 0 && !inOutputPlaceholder[k]) {
      violation(counts, 'left a broken placeholder', describe());
      break;
    }
  }
  const ids = new Set();
  for (let k = 0; k < letter.length; k++) if (inPlaceholder[k] && !branchKept[k]) ids.add(inPlaceholder[k]);
  if (ids.size) violation(counts, 'took out part of a placeholder in the letter', describe());
}

function note(seed, kind) {
  report.kinds[kind] = (report.kinds[kind] || 0) + 1;
  const bySeed = (report.kindsBySeed[seed] = report.kindsBySeed[seed] || {});
  bySeed[kind] = (bySeed[kind] || 0) + 1;
}

// Whether a value of more words is found, the way main found it, with its
// first word running on to the left or its last word to the right.
function runsOnIn(value, letter, map) {
  if (!value || !ofSeveralWords(value)) return false;
  const source = mainPattern(value);
  if (source === null) return false;
  const re = new RegExp(source, 'gi');
  let m;
  while ((m = re.exec(letter)) !== null) {
    re.lastIndex = m.index + 1;
    if (!edgeAt(map, m.index) || !edgeAt(map, m.index + m[0].length)) return true;
  }
  return false;
}

const ARABIC_LETTER = '[\\u0620-\\u064a]';

// What kinds of text the corpus has, so a generator that stopped making one
// shows in the counts.
function tally(seed, typed, letter) {
  const kind = (k) => note(seed, k);
  const has = (re) => re.test(letter);
  const map = wordMap(letter);
  const names = [typed.fname, typed.lname].filter((v) => v);
  const lower = letter.toLowerCase();
  if (has(/[一-鿿]/)) kind('chinese or japanese text');
  if (has(/[가-힯]/)) kind('korean text');
  if (has(/[؀-ۿ]/)) kind('arabic text');
  if (has(/[A-Za-z][一-鿿]|[一-鿿][A-Za-z]/)) kind('latin beside cjk');
  if (has(/[A-Za-z][؀-ۿ]|[؀-ۿ][A-Za-z]/)) kind('latin beside arabic');
  if (has(/[一-鿿][A-Za-z0-9._%+-]+@/)) kind('email inside cjk');
  if (has(/[؀-ۿ][A-Za-z0-9._%+-]+@|@[A-Za-z0-9.-]+[؀-ۿ]/)) kind('email beside arabic');
  if (has(/[A-Za-z0-9]\.[A-Za-z0-9._%+-]*@|@[A-Za-z0-9.-]+\.[a-z]+\.[a-z]+/)) kind('email with dotted continuation');
  if (has(/\u00a0/)) kind('nbsp');
  if (has(/\t/)) kind('tab');
  if (has(/\{\{[^{}]*\}\}/)) kind('existing placeholder');
  if (has(/['’]s(?![A-Za-z])/)) kind('lower case possessive');
  if (has(/['’]S(?![A-Za-z])/)) kind('upper case possessive');
  if (has(/Dear /)) kind('greeting');
  if (has(/Patient:? *[^\s\x00-\x7f]/)) kind('patient label before unspaced prose');
  if (has(/ , /)) kind('standalone comma');
  if (has(/\d{5}(?:\s{2,}|\t|\u00a0)\d{4}/)) kind('zip+4 with odd spacing');
  if (has(/[A-Z]{3,}/)) kind('upper case');
  if (typed.fname && /^\S$/u.test(typed.fname.trim())) kind('one character first name');
  if (typed.fname && /^\p{L}\.$/u.test(typed.fname)) kind('initial with a period');
  if (typed.fname && typed.fname.trim().length >= 8) kind('long first name');
  if (/[À-ÿ]/.test(names.join(''))) kind('accented name');
  if (/['’]/.test(typed.lname || '')) kind('apostrophe surname');
  if (/-/.test(typed.lname || '')) kind('hyphenated surname');
  if (/ /.test((typed.lname || '').trim())) kind('multi-part surname');
  if (/[一-鿿가-힯]/.test(names.join(''))) kind('cjk name');
  if (/^\d{5}[-\s]?\d{4}$/.test(typed.zip || '')) kind('typed zip+4');
  if (/[가-힯]/.test(names.join('')) && has(/[가-힯](?:님|씨|의|에게)/)) kind('korean name with a particle');
  for (const name of names) {
    if (!/[؀-ۿ]/.test(name)) continue;
    if (['و', 'ل', 'ب', 'وال'].some((p) => letter.indexOf(p + name) >= 0)) kind('arabic name with a prefix');
    if (new RegExp(escapeRegExp(name) + ARABIC_LETTER).test(letter)) kind('arabic name inside an arabic word');
  }
  const braces = /\{\{[^{}]*\}\}/g;
  let b;
  while ((b = braces.exec(letter)) !== null) {
    if (new RegExp('^' + SITE_PLACEHOLDER.source + '$').test(b[0])) continue;
    if (names.some((n) => n.trim() && b[0].toLowerCase().indexOf(n.trim().toLowerCase()) >= 0)) {
      kind('name in braces that are not a placeholder');
      break;
    }
  }
  if (runsOnIn(typed.street, letter, map)) kind('street runs on');
  if (runsOnIn(typed.email, letter, map)) kind('email runs on');
  if (typed.fname && typed.lname && runsOnIn(typed.fname + ' ' + typed.lname, letter, map)) kind('full name runs on');
  if (lower.indexOf('283 24th street') >= 0 && typed.street === '283 24th St') kind('the hint street printed long');
}

function runCase(seed, typed, letter) {
  report.cases++;
  tally(seed, typed, letter);
  const formMain = runForm(main, typed, letter);
  const formBranch = branch === null ? untouched(letter) : runForm(branch, typed, letter);
  const onTheForm = {fname: typed.fname, lname: typed.lname, email: typed.email, street: typed.street, zip: typed.zip};
  compare(
    report.form,
    'remove personal details',
    onTheForm,
    valuesOnTheForm(typed),
    letter,
    formMain,
    formBranch,
    typedSourcesForTheForm(typed),
  );
  const chatMain = runChat(main, typed, letter);
  const chatBranch = branch === null ? untouched(letter) : runChat(branch, typed, letter);
  compare(report.chat, 'chat', typed, valuesInTheChat(typed), letter, chatMain, chatBranch, null);
}

for (const [typed, letter] of HAND_WRITTEN) runCase('hand written', typed, letter);
for (const seed of spec.seeds) {
  const next = generator(seed);
  for (let i = 0; i < spec.count; i++) {
    const {typed, letter} = next();
    runCase(seed, typed, letter);
  }
}

process.stdout.write(JSON.stringify(report) + '\n');
