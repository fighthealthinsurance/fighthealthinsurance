'use strict';
// Load the send page's real script (appeal.ts) over a fake copy of the page
// and report what the person would have in the letter boxes. One scenario
// per process.
//
//   node appeal_send_page_behaviour.cjs <compiled dir> <scenario>
//
// <compiled dir> holds tsc's commonjs output of appeal.ts and what it
// imports (static/js/ under it, the shared JSON at its root). The scenario's
// input is a JSON object on stdin: the draft as the browser read it out of
// the page, what this browser's storage holds, the fax name the server put
// in the form, how the clipboard behaves, and what the person types.
//
// Writes one JSON object to stdout; everything the page logs is swallowed.

const fs = require('fs');
const path = require('path');
const Module = require('module');
const {buildPage, install} = require(path.join(__dirname, 'fake_page.cjs'));

const [, , compiledDir, scenarioName] = process.argv;
if (!compiledDir || !scenarioName) {
  throw new Error('usage: appeal_send_page_behaviour.cjs <compiled dir> <scenario>');
}
const SCRIPTS = path.join(compiledDir, 'static', 'js');
const input = JSON.parse(fs.readFileSync(0, 'utf8') || '{}');

// The ids are the real ones: test_the_send_page_keeps_your_letter.py checks
// them against the rendered appeal.html.
const MARKUP = `
<form id="share-form">
  <textarea id="scrubbed_appeal_text"></textarea>
</form>
<div id="pii-panel">
  <input id="pii_fname" />
  <input id="pii_lname" />
  <input id="pii_phone" />
  <input id="pii_email" />
  <input id="pii_street" />
  <input id="pii_city" />
  <input id="pii_state" />
  <input id="pii_zip" />
  <input id="pii_subscriber_id" />
  <input id="pii_group_id" />
</div>
<button id="descrub">Fill in your PII</button>
<div id="keep-a-copy">
  <button id="copy_appeal">Copy My Appeal</button>
  <button id="download_appeal">Download My Appeal</button>
  <span id="keep-a-copy-status"></span>
</div>
<form id="fax-form">
  <input id="id_name" />
  <textarea id="id_completed_appeal_text"></textarea>
  <button id="fax_appeal">Fax My Appeal</button>
</form>
<button id="print_appeal">Print My Appeal</button>
`;

const page = buildPage(MARKUP);
install(page);
for (const level of ['debug', 'log', 'info', 'warn', 'error']) {
  console[level] = () => {};
}

// Every box starts empty, the way a browser hands it over.
for (const el of page.body.querySelectorAll('input').concat(page.body.querySelectorAll('textarea'))) {
  el.value = '';
}

// This browser's storage, from the scenario.
const stored = Object.assign({}, input.stored || {});
const localStorage = {
  getItem: (key) => (Object.prototype.hasOwnProperty.call(stored, key) ? stored[key] : null),
  setItem: (key, value) => {
    stored[key] = String(value);
  },
  removeItem: (key) => {
    delete stored[key];
  },
  key: (i) => Object.keys(stored)[i] || null,
  get length() {
    return Object.keys(stored).length;
  },
};
global.window.localStorage = localStorage;
global.localStorage = localStorage;

// Browser bundles the page imports but these scenarios never reach.
const load = Module._load;
Module._load = function (request, ...rest) {
  if (request === 'pdfjs-dist') return {GlobalWorkerOptions: {}};
  if (request === 'jspdf') return {jsPDF: function () {}};
  return load.call(this, request, ...rest);
};

const doc = page.document;
const draft = doc.getElementById('scrubbed_appeal_text');
const letter = doc.getElementById('id_completed_appeal_text');
const faxName = doc.getElementById('id_name');
const status = doc.getElementById('keep-a-copy-status');

// The clipboard as the scenario says it behaves: "works", "refuses" (the
// browser turns the write down) or "missing" (no clipboard API at all).
const clipboard = [];
const navigatorStub =
  input.clipboard === 'missing'
    ? {}
    : {
        clipboard: {
          writeText: (text) => {
            if (input.clipboard === 'refuses') {
              return Promise.reject(new Error('NotAllowedError'));
            }
            clipboard.push(text);
            return Promise.resolve();
          },
        },
      };
Object.defineProperty(global, 'navigator', {value: navigatorStub, configurable: true, writable: true});
global.window.isSecureContext = input.secure !== false;

// The older copy: what is selected when it runs, and whether it worked.
const execCopies = [];
page.document.execCommand = (command) => {
  execCopies.push({
    command: command,
    selected: letter.value.slice(letter.selectionStart, letter.selectionEnd),
  });
  return input.execCopies !== false;
};
letter.setSelectionRange(0, 0);

// A download: the file's contents, and the address made for it.
const blobs = [];
const revoked = [];
class FakeBlob {
  constructor(parts, options) {
    this.parts = parts;
    this.type = options && options.type;
    blobs.push(this);
  }
}
Object.defineProperty(global, 'Blob', {value: FakeBlob, configurable: true, writable: true});
Object.defineProperty(global, 'URL', {
  value: {
    createObjectURL: (blob) => 'blob:example.test/' + blobs.indexOf(blob),
    revokeObjectURL: (url) => revoked.push(url),
  },
  configurable: true,
  writable: true,
});
// Print opens a window of its own; this one records what was written in it.
const printWindows = [];
global.window.open = (url) => {
  page.movedThePerson.push('window.open(' + url + ')');
  const child = buildPage('<div></div>');
  child.document.open = () => {};
  child.document.write = () => {};
  child.document.close = () => {};
  const win = {document: child.document, print: () => {}};
  printWindows.push(child);
  return win;
};

const links = [];
const createElement = page.document.createElement;
page.document.createElement = (tag) => {
  const el = createElement(tag);
  if (String(tag).toLowerCase() === 'a') links.push(el);
  return el;
};

function loadPage() {
  draft.value = input.draft || '';
  faxName.value = input.faxName || '';
  require(path.join(SCRIPTS, 'appeal.js'));
}

// Typing, as the browser reports it: the new value, then an input event.
function typeInto(id, value) {
  const el = doc.getElementById(id);
  el.value = value;
  if (typeof el.oninput === 'function') el.oninput({});
  el.dispatch('input', {});
}

// Lets a promise the page started settle before anything is read.
function settle() {
  return new Promise((resolve) => setImmediate(resolve));
}

function report(extra) {
  process.stdout.write(
    JSON.stringify(
      Object.assign(
        {
          draft: draft.value,
          letter: letter.value,
          faxName: faxName.value,
          status: status.textContent,
          moved: page.movedThePerson,
        },
        extra || {},
      ),
    ),
  );
}

const scenarios = {
  // The page as it loads: the finished letter is built from the draft.
  load() {
    loadPage();
    report();
  },
  // The person types in the draft, and the finished letter follows.
  'type-in-draft'() {
    loadPage();
    typeInto('scrubbed_appeal_text', input.typed);
    report();
  },
  // The person presses Copy My Appeal.
  async copy() {
    loadPage();
    if (input.letter !== undefined) letter.value = input.letter;
    doc.getElementById('copy_appeal').onclick({});
    await settle();
    report({
      clipboard: clipboard,
      execCopies: execCopies,
      focused: page.focused ? page.focused.id : null,
    });
  },
  // The person presses Download My Appeal, and a second passes.
  download() {
    loadPage();
    if (input.letter !== undefined) letter.value = input.letter;
    doc.getElementById('download_appeal').onclick({});
    const link = links[0] || null;
    const revokedAtOnce = revoked.slice();
    page.clock.advance(1000);
    report({
      files: blobs.map((blob) => ({text: blob.parts.join(''), type: blob.type})),
      link: link && {href: link.href, download: link.download, stillOnPage: link.parentNode !== null},
      revokedAtOnce: revokedAtOnce,
      revoked: revoked,
    });
  },
  // The person presses Print My Appeal on a letter with no blanks.
  print() {
    loadPage();
    if (input.letter !== undefined) letter.value = input.letter;
    doc.getElementById('print_appeal').onclick({});
    const printed = printWindows.map((child) =>
      child.body.querySelectorAll('pre').map((pre) => pre.textContent).join(''),
    );
    report({printed: printed});
  },
  // The fax form's name as the page loads, then after each thing typed.
  'fax-name'() {
    loadPage();
    const seen = [faxName.value];
    for (const [id, value] of input.steps || []) {
      typeInto(id, value);
      seen.push(faxName.value);
    }
    report({seen: seen});
  },
};

const scenario = scenarios[scenarioName];
if (!scenario) throw new Error('unknown scenario ' + scenarioName);
Promise.resolve(scenario()).catch((error) => {
  process.stderr.write(String((error && error.stack) || error));
  process.exit(1);
});
