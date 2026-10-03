'use strict';
// Drive the real compiled letter_details over a fake intake page and report
// what the person would see in About you. Driven by
// tests/sync/test_letter_details_behaviour.py, one scenario per process: the
// module keeps which fields it watches at module scope.
//
//   node letter_details_behaviour.cjs <compiled letter_details.js> '<spec json>'
//
// The spec is either {"find": [letter, ...]}, which reports what the rule
// finds in each letter, or one run of the page:
//   letter:  the letter's text
//   typed:   {field id: value} already in About you before the letter comes
//            (typed, or restored from this browser's storage)
//   arrive:  "paste"     a paste event, then the browser puts the text in;
//            "load"      the server rendered the letter into the box;
//            "restored"  this browser's storage put the letter back in the
//                        box before the page looked (not the server);
//            "type"      the letter typed in, one input event per keystroke;
//            "read"      a file read on this device, as scrub.ts reports it
//   edits:   [field id, ...] the person changes after the letter came
//   clears:  [field id, ...] the person empties after that
//   pasteAgain: text pasted under the letter after all of that (page two)
//
// Writes one JSON object to stdout. Every console call and every network
// call the page makes is recorded and reported, not printed.

const path = require('path');
const {buildPage, install} = require(path.join(__dirname, 'fake_page.cjs'));

const [, , modulePath, specJson] = process.argv;
if (!modulePath || !specJson) {
  throw new Error("usage: letter_details_behaviour.cjs <module> '<spec json>'");
}
const spec = JSON.parse(specJson);

// The letter's box and the About you step of scrub.html, with the real ids,
// classes and names. test_letter_details_behaviour.py checks these against
// the rendered page. The note's `hidden` is set below: the fake parser only
// reads quoted attributes.
const INTAKE_MARKUP = `
<form id="fuck_health_insurance_form">
  <section class="fhi-stack">
    <h2>Your denial letter</h2>
    <div class="fhi-field-group">
      <textarea name="denial_text" id="denial_text" class="fhi-field"></textarea>
    </div>
  </section>
  <section class="fhi-stack">
    <h2>About you</h2>
    <p class="fhi-note" id="details_from_letter" role="status" aria-live="polite"></p>
    <div class="together-form-group">
      <div class="fhi-field-group">
        <label for="store_fname" class="fhi-label">First name</label>
        <input type="text" id="store_fname" class="fhi-field" />
      </div>
      <div class="fhi-field-group">
        <label for="store_lname" class="fhi-label">Last name</label>
        <input type="text" id="store_lname" class="fhi-field" />
      </div>
    </div>
    <div class="fhi-field-group">
      <label for="email" class="fhi-label">Email</label>
      <input type="email" id="email" name="email" class="fhi-field" />
    </div>
    <div class="together-form-group">
      <div class="fhi-field-group">
        <label for="store_street" class="fhi-label">Street address</label>
        <input type="text" id="store_street" class="fhi-field" />
      </div>
      <div class="fhi-field-group">
        <label for="store_zip" class="fhi-label">ZIP code</label>
        <input type="text" id="store_zip" name="zip" class="fhi-field" />
      </div>
    </div>
  </section>
</form>
`;
const FIELDS = ['store_fname', 'store_lname', 'email', 'store_street', 'store_zip'];

const page = buildPage(INTAKE_MARKUP);
install(page);

const logs = [];
for (const level of ['debug', 'log', 'info', 'warn', 'error', 'trace', 'dir', 'table']) {
  console[level] = (...args) => logs.push([level].concat(args.map((a) => String(a))));
}
const network = [];
global.fetch = (...args) => {
  network.push(['fetch'].concat(args.map((a) => String(a))));
  return new Promise(() => {});
};
global.XMLHttpRequest = function () {
  network.push(['XMLHttpRequest']);
  this.open = () => {};
  this.send = (body) => network.push(['XMLHttpRequest.send', String(body)]);
};
// Node has a navigator of its own, behind a getter.
Object.defineProperty(global, 'navigator', {
  configurable: true,
  value: {
    sendBeacon: (...args) => {
      network.push(['sendBeacon'].concat(args.map((a) => String(a))));
      return true;
    },
  },
});

const details = require(path.resolve(modulePath));

if (spec.find) {
  process.stdout.write(
    JSON.stringify({found: spec.find.map((letter) => details.findDetailsInLetter(letter)), logs}) + '\n',
  );
  process.exit(0);
}

const doc = page.document;
const box = doc.getElementById('denial_text');
const note = doc.getElementById('details_from_letter');
note.hidden = true;
box.value = '';
box.defaultValue = '';
for (const id of FIELDS) {
  const field = doc.getElementById(id);
  field.value = (spec.typed || {})[id] || '';
  field.defaultValue = '';
}

// What the page's storage helper was handed, in order.
const remembered = [];
const remember = (id, value) => remembered.push([id, value]);

function fieldsNow() {
  const out = {};
  for (const id of FIELDS) out[id] = doc.getElementById(id).value;
  return out;
}

const letter = spec.letter || '';
let beforeTheTextLanded = null;
if (spec.arrive === 'load') {
  box.value = letter;
  box.defaultValue = letter;
  details.watchLetterForDetails(box, remember);
} else if (spec.arrive === 'restored') {
  box.value = letter;
  details.watchLetterForDetails(box, remember);
} else {
  details.watchLetterForDetails(box, remember);
  if (spec.arrive === 'paste') {
    // The event comes first; the browser puts the text in after it.
    box.dispatch('paste', {});
    box.value += letter;
    beforeTheTextLanded = fieldsNow();
    page.clock.advance(0);
  } else if (spec.arrive === 'type') {
    for (const ch of letter) {
      box.value += ch;
      box.dispatch('input', {});
    }
    page.clock.advance(1000);
  } else if (spec.arrive === 'read') {
    box.value += letter;
    details.fillDetailsFromLetter(box.value, remember);
  } else {
    throw new Error('unknown arrival ' + spec.arrive);
  }
}

const afterFill = fieldsNow();
for (const id of spec.edits || []) {
  const field = doc.getElementById(id);
  field.value = field.value + 'x';
  field.dispatch('input', {});
}
for (const id of spec.clears || []) {
  const field = doc.getElementById(id);
  field.value = '';
  field.dispatch('input', {});
}
if (spec.pasteAgain !== undefined) {
  box.dispatch('paste', {});
  box.value += spec.pasteAgain;
  page.clock.advance(0);
}

function hintFor(id) {
  const field = doc.getElementById(id);
  const hint = doc.getElementById(id + '_from_letter');
  if (!hint) return null;
  const siblings = field.parentNode.childNodes;
  return {
    text: hint.textContent,
    tag: hint.tagName.toLowerCase(),
    className: hint.className,
    rightAfterTheField: siblings.indexOf(hint) === siblings.indexOf(field) + 1,
  };
}

const hints = {};
const describedBy = {};
for (const id of FIELDS) {
  hints[id] = hintFor(id);
  describedBy[id] = doc.getElementById(id).getAttribute('aria-describedby') || null;
}

process.stdout.write(
  JSON.stringify({
    beforeTheTextLanded,
    fields: afterFill,
    fieldsAtEnd: fieldsNow(),
    hints,
    describedBy,
    note: {text: note.textContent, hidden: note.hidden === true},
    remembered,
    logs,
    network,
    sockets: page.sockets.length,
    leftThePage: page.movedThePerson,
  }) + '\n',
);
