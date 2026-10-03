'use strict';
// Drive the real compiled letter_details over a fake intake page and report
// what the person would see in About you. Driven by
// tests/sync/test_letter_details_behaviour.py, one scenario per process: the
// module keeps which fields it watches at module scope.
//
//   node letter_details_behaviour.cjs <compiled letter_details.js> '<spec json>'
//
// The other compiled modules (pdf_text.js, scrub.js and what it imports,
// user_info_storage.js) sit beside it. The spec is one of:
//   {"find": [letter, ...]}       what the rule finds in each letter
//   {"pdfText": [[item, ...], ...]} the text pdf_text makes of each page's
//                                 pdf.js text items
//   {"scrubPersonalInfo": [[message, userInfo], ...]}
//                                 what the chat's scrubPersonalInfo makes of
//                                 each message
//   {"page": true, ...}           the whole intake script (scrub.js), which
//                                 sets the page up as it loads; see runThePage
// or one run of letter_details on its own:
//   letter:  the letter's text
//   typed:   {field id: value} already in About you before the letter comes
//            (typed, or restored from this browser's storage)
//   types:   {field id: value} the person types, one input event, once the
//            page is watching and before the letter comes
//   resets:  [field id, ...] emptied with no input event, the way a form
//            reset does, once the page is watching and before the letter
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

// The letter's step, the About you step and the four boxes of scrub.html,
// with the real ids, classes and names. test_letter_details_behaviour.py
// checks these against the rendered page. The note's `hidden` is set below:
// the fake parser only reads quoted attributes.
const INTAKE_MARKUP = `
<form id="fuck_health_insurance_form">
  <section class="fhi-stack">
    <h2>Your denial letter</h2>
    <div id="image_select_magic" class="fhi-file-pick">
      <input id="uploader" type="file" multiple="true" class="fhi-visually-hidden" />
    </div>
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
    <div class="fhi-field-group">
      <button type="button" id="scrub-2" class="fhi-button fhi-button-secondary">Remove personal details</button>
    </div>
  </section>
  <section>
    <input type="checkbox" id="pii" name="pii" class="fhi-check" />
    <input type="checkbox" id="privacy" name="privacy" class="fhi-check" />
    <input type="checkbox" id="tos" name="tos" class="fhi-check" />
    <input type="checkbox" id="personalonly" name="personalonly" class="fhi-check" />
    <input type="checkbox" id="persistence_enabled" class="fhi-check" />
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

const built = path.dirname(path.resolve(modulePath));

if (spec.pdfText) {
  const {textFromPDFItems} = require(path.join(built, 'pdf_text.js'));
  process.stdout.write(JSON.stringify({texts: spec.pdfText.map(textFromPDFItems), logs}) + '\n');
  process.exit(0);
}

if (spec.scrubPersonalInfo) {
  const {scrubPersonalInfo} = require(path.join(built, 'user_info_storage.js'));
  const scrubbed = spec.scrubPersonalInfo.map(([message, userInfo]) => scrubPersonalInfo(message, userInfo));
  process.stdout.write(JSON.stringify({scrubbed, logs}) + '\n');
  process.exit(0);
}

const doc = page.document;
const box = doc.getElementById('denial_text');
const note = doc.getElementById('details_from_letter');
note.hidden = true;
box.value = '';
box.defaultValue = '';
for (const input of doc.querySelectorAll('input')) {
  input.value = (spec.typed || {})[input.id] || '';
  input.defaultValue = '';
}

function fieldsNow() {
  const out = {};
  for (const id of FIELDS) out[id] = doc.getElementById(id).value;
  return out;
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

function whatAboutYouShows() {
  const hints = {};
  const describedBy = {};
  for (const id of FIELDS) {
    hints[id] = hintFor(id);
    describedBy[id] = doc.getElementById(id).getAttribute('aria-describedby') || null;
  }
  return {hints, describedBy, note: {text: note.textContent, hidden: note.hidden === true}};
}

// The whole intake script, which sets the page up as it loads (setupScrub in
// scrub.ts), over the same markup. The spec:
//   letter:      put in the box by the server before the script runs
//   typed:       (as above) in About you before the script runs
//   storageFull: this browser's storage takes no writes (QuotaExceededError)
//   storageBlocked: the browser blocks this site's storage, so reading
//                window.localStorage throws (SecurityError)
//   fillThrows:  the About you fill (letter_details) throws whatever it is
//                asked to do
//   pdf:         [[item, ...], ...] a PDF with a text layer, one list of
//                pdf.js text items per page, chosen with the file button
//                once the page is set up
//   removePersonalDetails: the person presses Remove personal details last
// The document finishes loading (DOMContentLoaded) once the script has run.
// Reports what the page wired up, what About you shows, what the box holds,
// what was stored, and any promise the page let fail with no one to catch
// it. pdf.js, tesseract and the on-device model are not loaded; pdf.js is
// stood in for by the pages above.
async function runThePage() {
  const unhandled = [];
  process.on('unhandledRejection', (error) => unhandled.push(String(error && error.name)));
  const store = new Map();
  const localStorage = {
    getItem: (key) => (store.has(key) ? store.get(key) : null),
    setItem: (key, value) => {
      if (spec.storageFull) {
        const full = new Error('The quota has been exceeded.');
        full.name = 'QuotaExceededError';
        throw full;
      }
      store.set(key, String(value));
    },
    removeItem: (key) => store.delete(key),
    get length() {
      return store.size;
    },
    key: (index) => Array.from(store.keys())[index] ?? null,
  };
  if (spec.storageBlocked) {
    const blocked = () => {
      const error = new Error('The operation is insecure.');
      error.name = 'SecurityError';
      throw error;
    };
    Object.defineProperty(global.window, 'localStorage', {configurable: true, get: blocked});
    Object.defineProperty(global, 'localStorage', {configurable: true, get: blocked});
  } else {
    global.window.localStorage = localStorage;
    global.localStorage = localStorage;
  }
  // A form reaches its controls by name.
  const form = doc.getElementById('fuck_health_insurance_form');
  for (const control of form.querySelectorAll('input').concat(form.querySelectorAll('textarea'))) {
    const name = control.getAttribute('name');
    if (name) form[name] = control;
  }
  const pages = spec.pdf || [];
  const pdfjs = {
    GlobalWorkerOptions: {},
    getDocument: () => ({
      promise: Promise.resolve({
        numPages: pages.length,
        getPage: async (number) => ({
          getTextContent: async () => ({items: pages[number - 1]}),
          cleanup: () => {},
        }),
        destroy: async () => {},
      }),
    }),
  };
  global.FileReader = function () {
    this.readAsArrayBuffer = () => {
      Promise.resolve().then(() => {
        this.result = new ArrayBuffer(8);
        this.onload();
      });
    };
  };
  const Module = require('module');
  const load = Module._load;
  Module._load = function (request, ...rest) {
    if (request === 'pdfjs-dist') return pdfjs;
    if (request === 'tesseract.js') return {};
    if (request === './letter_details' && spec.fillThrows) {
      const broken = () => {
        throw new TypeError('the fill broke');
      };
      return {watchLetterForDetails: broken, fillDetailsFromLetter: broken};
    }
    return load.call(this, request, ...rest);
  };

  box.value = spec.letter || '';
  box.defaultValue = spec.letter || '';
  let setupError = null;
  try {
    require(path.join(built, 'scrub.js'));
  } catch (error) {
    setupError = error.name + ': ' + error.message;
  }
  let domReadyError = null;
  try {
    page.fireDomReady();
  } catch (error) {
    domReadyError = error.name + ': ' + error.message;
  }

  const uploader = doc.getElementById('uploader');
  if (spec.pdf) {
    uploader.files = [{name: 'letter.pdf', type: 'application/pdf'}];
    uploader.dispatch('change', {target: uploader});
    // The read is promises all the way down; let them settle.
    for (let turn = 0; turn < 50; turn += 1) {
      await new Promise((resolve) => setImmediate(resolve));
    }
  }

  let removeError = null;
  if (spec.removePersonalDetails) {
    try {
      doc.getElementById('scrub-2').onclick();
    } catch (error) {
      removeError = error.name + ': ' + error.message;
    }
  }

  const listeners = (element, name) => (element.listeners[name] || []).length;
  return Object.assign(
    {
      setupError,
      domReadyError,
      removeError,
      unhandled,
      remembering: doc.getElementById('persistence_enabled').checked === true,
      wired: {
        upload: listeners(uploader, 'change'),
        paste: listeners(box, 'paste'),
        removePersonalDetails: typeof doc.getElementById('scrub-2').onclick === 'function',
        submitCheck: listeners(form, 'submit'),
      },
      fields: fieldsNow(),
      box: box.value,
      stored: Array.from(store.keys()),
      logs,
      network,
    },
    whatAboutYouShows(),
  );
}

if (spec.page) {
  runThePage().then(
    (result) => process.stdout.write(JSON.stringify(result) + '\n'),
    (error) => {
      process.stderr.write(String(error && error.stack) + '\n');
      process.exit(1);
    },
  );
  return;
}

const details = require(path.resolve(modulePath));

if (spec.find) {
  process.stdout.write(
    JSON.stringify({found: spec.find.map((letter) => details.findDetailsInLetter(letter)), logs}) + '\n',
  );
  process.exit(0);
}

// What the page's storage helper was handed, in order.
const remembered = [];
const remember = (id, value) => remembered.push([id, value]);

// Once the page is watching, before the letter comes.
function beforeTheLetter() {
  for (const [id, value] of Object.entries(spec.types || {})) {
    const field = doc.getElementById(id);
    field.value = value;
    field.dispatch('input', {});
  }
  for (const id of spec.resets || []) {
    doc.getElementById(id).value = '';
  }
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
  beforeTheLetter();
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

process.stdout.write(
  JSON.stringify(
    Object.assign(
      {
        beforeTheTextLanded,
        fields: afterFill,
        fieldsAtEnd: fieldsNow(),
        remembered,
        logs,
        network,
        sockets: page.sockets.length,
        leftThePage: page.movedThePerson,
      },
      whatAboutYouShows(),
    ),
  ) + '\n',
);
