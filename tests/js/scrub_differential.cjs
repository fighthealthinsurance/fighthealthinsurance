'use strict';
// Run main's scrubbers and this branch's over the same generated letters and
// report where this branch leaves a personal detail that main took out.
// Driven by tests/sync/test_scrub_never_leaves_what_main_removed.py.
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
//   count    how many generated cases, after the hand-written ones
//   seed     for the generator, so every run makes the same letters
//
// Each case is what was typed (first and last name, email, street, ZIP and,
// for the chat, city) and a letter. Both run through Remove personal
// details (scrub_scrub.ts clean, on a page with the intake page's inputs)
// and the chat's scrubPersonalInfo (user_info_storage.ts).
//
// What is checked, per case and per scrubber:
//
// 1. Nothing main took out is left. Every String.prototype.replace the
//    scrubbers make on the text is recorded, so each character of the
//    output is known to be a character of the letter or one put in. For
//    each match main replaced, every letter, mark or digit of the letter it
//    took out must be gone from the branch's output too, unless the match
//    sat strictly inside a longer word: a letter or digit of the same word
//    directly before or after it in the letter. "Same word" counts a mark
//    as part of the letter it sits on, and a change between a script with
//    capitals (or a digit) and one without, or two letters of a script
//    without capitals (Chinese, Japanese, Thai, ...), as a word boundary:
//    "Ann" in "患者Ann" and 王小明 in "患者王小明的申请" stand as words. That
//    covers main's label rules too ("Dear Bob Roe" with nothing typed).
//    Matches from a value nobody typed (main also took the tick boxes'
//    "checked" out) are not held to this.
// 2. Nothing main kept is taken out. A word of the letter (a run of letters
//    and digits of a script with capitals, or one letter of a script
//    without) that main left whole must be left whole by the branch, unless
//    it is one of the words of a value the scrubber looks for (what was
//    typed, and for Remove personal details each box's value run together
//    with another's, which main looked for too).
// 3. No placeholder is written into. The branch never replaces a character
//    of a {{PLACEHOLDER}}, one already in the letter or one it put in, nor
//    puts text inside one, and every {{ in its output closes.
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

// Where each character of the text came from: its index in the letter, or
// -1 for one a replacement put in; and which placeholder it is part of, 0
// for none.
let tracker = null;
let nextPlaceholderId = 1;

function placeholderIds(text) {
  const ids = new Array(text.length).fill(0);
  const re = /\{\{[^{}]*\}\}/g;
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

const CASED_OR_DIGIT = /^[\p{Lu}\p{Ll}\p{Lt}\p{N}]$/u;
const LETTER = /^\p{L}$/u;
const MARK = /^\p{M}$/u;

// "cased" (a letter with a capital form, or a digit), "caseless" (a letter
// of a script without capitals), "mark", or null (not part of a word).
function kindOf(ch) {
  if (ch === '') return null;
  if (MARK.test(ch)) return 'mark';
  if (CASED_OR_DIGIT.test(ch)) return 'cased';
  if (LETTER.test(ch)) return 'caseless';
  return null;
}

function charAt(text, i) {
  return i >= text.length ? '' : String.fromCodePoint(text.codePointAt(i));
}

function charBefore(text, i) {
  if (i <= 0) return '';
  const low = text.charCodeAt(i - 1);
  if (low >= 0xdc00 && low <= 0xdfff && i >= 2) {
    const high = text.charCodeAt(i - 2);
    if (high >= 0xd800 && high <= 0xdbff) return text.slice(i - 2, i);
  }
  return text.slice(i - 1, i);
}

// The kind of the word character before i, through the marks on it.
function kindBefore(text, i) {
  let at = i;
  while (at > 0) {
    const ch = charBefore(text, at);
    const kind = kindOf(ch);
    if (kind !== 'mark') return kind;
    at -= ch.length;
  }
  return null;
}

// Whether the characters either side of i are one word.
function inOneWord(text, i) {
  const after = kindOf(charAt(text, i));
  if (after === null) return false;
  const before = kindBefore(text, i);
  if (before === null) return false;
  if (after === 'mark') return true;
  return before === 'cased' && after === 'cased';
}

// The words of a text: runs of letters (with capitals) and digits, and each
// letter of a script without capitals on its own, with the marks on them.
function wordsOf(text) {
  const words = [];
  let current = null;
  for (let i = 0; i < text.length; ) {
    const ch = charAt(text, i);
    const kind = kindOf(ch);
    if (kind === 'cased' && current !== null && current.kind === 'cased') {
      current.end = i + ch.length;
    } else if (kind === 'cased' || kind === 'caseless') {
      current = {start: i, end: i + ch.length, kind};
      words.push(current);
    } else if (kind === 'mark' && current !== null) {
      current.end = i + ch.length;
    } else {
      current = null;
    }
    i += ch.length;
  }
  return words;
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
  'Ann Ann', 'Anna', 'Lee Ann', 'Ann王', 'Mary Ann Lee',
];
const LAST = [
  'Doe', 'Day', 'Smith', 'Smith-Jones', "O'Neill", 'O’Brien', 'de la Cruz', 'Núñez',
  'van der Berg', '王', '田中', '김', 'Name', 'Price', 'Patient', 'Doering', 'Lee', 'Li',
  'Ng', 'Group', 'Member', 'Ann', 'Same', 'Jones Jr.', "D'Angelo-Ruiz", 'Müller',
  'MacDonald', 'St. John', 'Doe,', 'X', 'Lee Ann', 'Smith Smith', 'Ann Lee',
];
const EMAIL = [
  'ann@example.com', 'ann.doe@example.com', 'asmith@example.org', 'j.doe+fhi@mail.example.net',
  'ann_doe@example.com', 'sam@day.example', 'min.su@example.kr', 'jose.nunez@example.com',
  'x@y.z', 'ANN@EXAMPLE.COM',
];
const STREET = [
  '123 Main St', '123 Main St , Apt 4B', '123 Main St, Apt 4B', '123 Sample Street Apt 4B',
  '283 24th St', '9 Oak Ave.', '#4B 12 Elm Rd', '123 王府井大街', "1 Rue de l'Église",
  '500 N. State St', 'PO Box 12', '12 Day St', '4 Sam Ct', '77 Ann St.', '10 Downing St',
  '1600 Pennsylvania Ave NW', '123 Main St. ,  Apt. 4B', '5 Ave . B', '1 José St',
  '10 Main St', '9 Lee Ann Ct', '62701 Ann St',
];
const ZIP = ['62701', '62701-1234', '62701 1234', '94103', 'SW1A 1AA', '02134', '1234', '627011234', '62701\t1234'];
const CITY = [
  'Springfield', 'Mesa', 'Day', 'Saint-Denis', 'São Paulo', '北京', 'Same', 'Ann Arbor',
  'St Louis', 'San José', 'Lee',
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
];

function generator(seed) {
  const rng = mulberry32(seed);
  const pick = (list) => list[Math.floor(rng() * list.length)];
  const chance = (p) => rng() < p;

  const transforms = [
    (v) => v.toUpperCase(),
    (v) => v.toLowerCase(),
    (v) => nativeReplace.call(v, /\S+/g, (w) => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase()),
    (v) => nativeReplace.call(v, / /g, () => pick([' ', '  ', '\n', '\t', ' ', ' \n'])),
    (v) => nativeReplace.call(v, /['’]/g, (c) => (c === "'" ? '’' : "'")),
    (v) => v + pick(["'s", "'S", '’s', '’S', "'"]),
    (v) => (chance(0.5) ? pick(['Jo', 'x', 'Mc', '1', 'É', 'é']) + v : v + pick(['ette', 's', 'y', '1', 'ing', 'ual', 'é', '́'])),
    (v) => {
      const [a, b] = pick([['(', ')'], ['"', '"'], ['“', '”'], ['-', '-'], ['_', '_'], ['<', '>'], ['[', ']'], ['#', ''], ['', ','], ['', '.'], ['', ';'], ['', ':'], ['/', '/']]);
      return a + v + b;
    },
    (v) => {
      const [a, b] = pick([['患者', '的申请'], ['我的电子邮箱是', '。'], ['邮编', '号'], ['', '患有2型糖尿病。'], ['김', '님께'], ['เรียนคุณ', ''], ['و', '']]);
      return a + v + b;
    },
    (v) => {
      const [a, b] = pick([['first.', ''], ['', '.au'], ['', '.com'], ['', '.'], ['', '@x'], ['mailto:', '']]);
      return a + v + b;
    },
    (v) => v + pick(['-1234', ' 1234', '  1234', '\t1234', ' 1234', '1234', '\n1234']),
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
    (t) => t.R('fname') + pick([' ', '\n', '  ', '\t', ' ', '']) + t.R('lname'),
    (t) => t.R('lname') + pick([', ', ',', ' ', '\n']) + t.R('fname'),
    (t) => t.R('street') + pick(['\n', ', ', ' ', '']) + t.city() + pick([', IL ', ' IL ', '\n', ' ']) + t.R('zip'),
    (t) => pick(['Write to ', 'Email: ', '我的电子邮箱是', '', '<', 'mailto:']) + t.R('email') + pick(['.', '', '。', '>', '.au', ',']),
    (t) => pick(['IL ', 'ZIP: ', '邮编', 'Springfield, IL ']) + t.R('zip') + pick(['', '-1234', ' 1234', ' 1234']),
    () => pick(MEDICAL),
    () => pick(MEDICAL),
    () => pick(PLACEHOLDER_LINES),
    (t) => pick(['{{FIRST_NAME}}', '{{PATIENT_NAME}} ', '{{LAST_NAME}}, ']) + t.R(pick(['fname', 'lname'])),
    (t) => t.R(pick(['fname', 'lname'])) + pick(['{{LAST_NAME}}', ' {{FIRST_NAME}}', '{{Your Email Address}}']),
    () => pick(['Subscriber ID: XYZ000000', 'Group number: G12345', 'Group ID: 55555']),
    (t) => t.R(pick(['fname', 'lname', 'email', 'street', 'zip', 'city'])),
    (t) => '患者' + t.R('fname') + pick([' ', '']) + t.R('lname') + '的申请',
    (t) => t.R('lname') + t.R('fname') + pick(['様', '님께', '的申请', '']),
  ];
  const separators = ['\n', ' ', '\n\n', '', '. ', '。', '\t', ', '];

  return function next() {
    const typed = typedSet();
    const ctx = {
      R: (field) => (typed[field] ? render(typed[field]) : pick(UNTYPED)),
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

// The cases Codex found, and the gaps it listed, first.
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
  [{zip: '62701 1234'}, '62701 1234'],
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
  [{fname: 'Ann'}, 'Joann Ann ann ANN Anń éAnn Ann_Doe'],
  [{street: '123 王府井大街'}, '123 王府井大街123 王府井大街'],
];

// ------------------------------------------------------------ one scrubber

// What main's Remove personal details looks for: each store_ box's value,
// and it run together with every box that has a value (of the typed ones;
// the tick boxes' are left out), and the email box's value.
function valuesOnTheForm(typed) {
  const stores = [typed.fname, typed.lname, typed.street, typed.zip].filter((v) => v);
  const all = stores.concat(typed.email ? [typed.email] : []);
  const values = all.slice();
  for (const v of stores) for (const w of all) values.push(v + w);
  return values;
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
  kinds: {},
  form: newCounts(),
  chat: newCounts(),
};

function newCounts() {
  return {
    mainMatches: 0,
    mainMatchesInsideAWord: 0,
    mainMatchesFromUntypedBoxes: 0,
    charactersChecked: 0,
    wordsMainKeptChecked: 0,
    // {kind: {count, examples}}
    violations: {},
  };
}

function violation(counts, kind, details) {
  const found = (counts.violations[kind] = counts.violations[kind] || {count: 0, examples: []});
  found.count++;
  if (found.examples.length < EXAMPLES) found.examples.push(details);
}

// typedValues: every value the scrubber looks for, so its words are the
// person's (for Remove personal details, each box's value run together with
// every other's too, as main also looked for them).
function compare(counts, which, typed, typedValues, letter, mainRun, branchRun, typedSources) {
  const mainKept = new Uint8Array(letter.length);
  for (const o of mainRun.origin) if (o >= 0) mainKept[o] = 1;
  const branchKept = new Uint8Array(letter.length);
  for (const o of branchRun.origin) if (o >= 0) branchKept[o] = 1;
  const inPlaceholder = placeholderIds(letter);
  const wordCharacter = new Uint8Array(letter.length);
  for (const w of wordsOf(letter)) for (let k = w.start; k < w.end; k++) wordCharacter[k] = 1;
  const describe = () => ({scrubber: which, typed, letter, main: mainRun.text, branch: branchRun.text});

  // 1. Nothing main took out is left.
  for (const s of mainRun.steps) {
    const label = MAIN_LABEL_SOURCES.has(s.source);
    const fromTyped = label || typedSources === null || typedSources.has(s.source);
    for (const c of s.changes) {
      if (c.removed.length === 0) continue;
      counts.mainMatches++;
      if (!fromTyped) {
        counts.mainMatchesFromUntypedBoxes++;
        continue;
      }
      const insideAWord =
        (c.firstOrigin >= 0 && inOneWord(letter, c.firstOrigin)) ||
        (c.lastOrigin >= 0 && inOneWord(letter, c.lastOrigin + 1));
      if (insideAWord) {
        counts.mainMatchesInsideAWord++;
        continue;
      }
      const left = [];
      for (const o of c.removed) {
        if (!wordCharacter[o] || inPlaceholder[o]) continue;
        counts.charactersChecked++;
        if (branchKept[o]) left.push(o);
      }
      if (left.length) {
        violation(counts, 'left what main took out', Object.assign(describe(), {
          mainMatched: c.matched,
          left: left.map((o) => letter[o]).join(''),
        }));
      }
    }
  }

  // 2. Nothing main kept is taken out.
  const typedWords = [];
  for (const value of typedValues) {
    if (!value) continue;
    for (const w of wordsOf(value)) typedWords.push(escapeRegExp(value.slice(w.start, w.end)));
  }
  const isTyped = typedWords.length ? new RegExp('^(?:' + typedWords.join('|') + ')$', 'iu') : null;
  for (const w of wordsOf(letter)) {
    let keptByMain = true;
    let keptByBranch = true;
    let inAPlaceholder = false;
    for (let k = w.start; k < w.end; k++) {
      if (!mainKept[k]) keptByMain = false;
      if (!branchKept[k]) keptByBranch = false;
      if (inPlaceholder[k]) inAPlaceholder = true;
    }
    const text = letter.slice(w.start, w.end);
    if (!keptByMain || inAPlaceholder || (isTyped !== null && isTyped.test(text))) continue;
    counts.wordsMainKeptChecked++;
    if (!keptByBranch) {
      violation(counts, 'took out a word main kept', Object.assign(describe(), {word: text}));
    }
  }

  // 3. No placeholder is written into.
  for (const s of branchRun.steps) {
    for (const c of s.changes) {
      if (c.touches) {
        violation(counts, 'wrote into a placeholder', Object.assign(describe(), {matched: c.matched}));
      }
    }
  }
  const stray = nativeReplace.call(branchRun.text, /\{\{[^{}]*\}\}/g, '');
  if (stray.indexOf('{{') >= 0 || stray.indexOf('}}') >= 0) {
    violation(counts, 'left a broken placeholder', describe());
  }
  const ids = new Set();
  for (let k = 0; k < letter.length; k++) if (inPlaceholder[k] && !branchKept[k]) ids.add(inPlaceholder[k]);
  if (ids.size) violation(counts, 'took out part of a placeholder in the letter', describe());
}

function note(kind) {
  report.kinds[kind] = (report.kinds[kind] || 0) + 1;
}

// What kinds of text the corpus has, so a generator that stopped making one
// shows in the counts.
function tally(typed, letter) {
  const has = (re) => re.test(letter);
  if (has(/[一-鿿]/)) note('chinese or japanese text');
  if (has(/[가-힯]/)) note('korean text');
  if (has(/[A-Za-z][一-鿿]|[一-鿿][A-Za-z]/)) note('latin beside cjk');
  if (has(/[一-鿿][A-Za-z0-9._%+-]+@/)) note('email inside cjk');
  if (has(/[A-Za-z0-9]\.[A-Za-z0-9._%+-]*@|@[A-Za-z0-9.-]+\.[a-z]+\.[a-z]+/)) note('email with dotted continuation');
  if (has(/ /)) note('nbsp');
  if (has(/\t/)) note('tab');
  if (has(/\{\{[^{}]*\}\}/)) note('existing placeholder');
  if (has(/['’]s(?![A-Za-z])/)) note('lower case possessive');
  if (has(/['’]S(?![A-Za-z])/)) note('upper case possessive');
  if (has(/Dear /)) note('greeting');
  if (has(/Patient:? *[^\s\x00-\x7f]/)) note('patient label before unspaced prose');
  if (has(/ , /)) note('standalone comma');
  if (has(/\d{5}(?:\s{2,}|\t| )\d{4}/)) note('zip+4 with odd spacing');
  if (has(/[A-Z]{3,}/)) note('upper case');
  if (typed.fname && /^\S$/u.test(typed.fname.trim())) note('one character first name');
  if (typed.fname && typed.fname.trim().length >= 8) note('long first name');
  if (/[À-ÿ]/.test((typed.fname || '') + (typed.lname || ''))) note('accented name');
  if (/['’]/.test(typed.lname || '')) note('apostrophe surname');
  if (/-/.test(typed.lname || '')) note('hyphenated surname');
  if (/ /.test((typed.lname || '').trim())) note('multi-part surname');
  if (/[一-鿿가-힯]/.test((typed.fname || '') + (typed.lname || ''))) note('cjk name');
  if (/^\d{5}[-\s]?\d{4}$/.test(typed.zip || '')) note('typed zip+4');
}

function runCase(typed, letter) {
  report.cases++;
  tally(typed, letter);
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
  compare(report.chat, 'chat', typed, Object.values(typed), letter, chatMain, chatBranch, null);
}

for (const [typed, letter] of HAND_WRITTEN) runCase(typed, letter);
const next = generator(spec.seed);
for (let i = 0; i < spec.count; i++) {
  const {typed, letter} = next();
  runCase(typed, letter);
}

process.stdout.write(JSON.stringify(report) + '\n');
