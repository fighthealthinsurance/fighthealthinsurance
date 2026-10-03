'use strict';
// Drive the blank-letter check over a fake page and report what the person
// would be looking at. One scenario per process.
//
//   node letter_placeholders_behaviour.cjs <compiled dir> <scenario>
//
// <compiled dir> holds tsc's commonjs output of letter_placeholders.ts and
// escalation_packet_review.ts (static/js/ under it, the shared JSON at its
// root). The print scenarios load the regulator letter review page's own
// script, which wires its Print button at load; the fax scenarios call the
// check the appeal page's fax form calls. The "find" scenario reads a JSON
// list of letters on stdin and prints what the browser finds in each.
//
// Writes one JSON object to stdout; everything the page logs is swallowed.

const fs = require('fs');
const path = require('path');
const {buildPage, install} = require(path.join(__dirname, 'fake_page.cjs'));

const [, , compiledDir, scenarioName] = process.argv;
if (!compiledDir || !scenarioName) {
  throw new Error('usage: letter_placeholders_behaviour.cjs <compiled dir> <scenario>');
}
const SCRIPTS = path.join(compiledDir, 'static', 'js');

// The ids are the real ones: test_letter_placeholders.py checks them against
// escalation_packet_review.html and appeal.html.
const MARKUP = `
<div id="send-it">
  <textarea id="id_completed_appeal_text"></textarea>
  <button id="print_appeal">Print this letter</button>
</div>
<form id="fax-form">
  <textarea id="fax-letter"></textarea>
  <button id="fax_appeal">Fax My Appeal</button>
</form>
`;

const WITH_BLANKS = 'Dear Example Health,\n\nI am [Your Name], member {{SCSID}}.\n\nSincerely,\n[Your Name]';
const COMPLETE = 'Dear Example Health,\n\nI am Pat Example, member 12345.\n\nSincerely,\nPat Example';
const WITH_MARKUP = 'Dear Example Health, from {{<img src=x onerror=alert(1)>}}.';

const page = buildPage(MARKUP);
install(page);
// A real window.open hands back a window; null is what a blocked popup
// gives, and the print code stops there, so only the attempt is recorded.
global.window.open = (url) => {
  page.movedThePerson.push('window.open(' + url + ')');
  return null;
};
for (const level of ['debug', 'log', 'info', 'warn', 'error']) {
  console[level] = () => {};
}

const doc = page.document;
const letter = doc.getElementById('id_completed_appeal_text');
const printButton = doc.getElementById('print_appeal');
const faxLetter = doc.getElementById('fax-letter');
const faxButton = doc.getElementById('fax_appeal');

function describe(el) {
  if (!el) return null;
  return el.id ? '#' + el.id : el.tagName.toLowerCase();
}

function noticeSnapshot(id) {
  const notice = doc.getElementById(id);
  if (!notice) return null;
  const heading = doc.getElementById(id + '-heading');
  return {
    className: notice.className,
    tabindex: notice.getAttribute('tabindex'),
    labelledBy: notice.getAttribute('aria-labelledby'),
    heading: heading ? heading.textContent : null,
    items: notice.querySelectorAll('li').map((li) => li.textContent),
    buttons: notice.querySelectorAll('button').map((b) => ({
      text: b.textContent,
      type: b.getAttribute('type'),
    })),
    text: notice.textContent,
    sitsBefore: describe(notice.nextSibling),
  };
}

function count(id) {
  let n = 0;
  const walk = (node) => {
    for (const child of node.childNodes || []) {
      if (child.nodeType !== 1) continue;
      if (child.id === id) n += 1;
      walk(child);
    }
  };
  walk(page.body);
  return n;
}

function pressNoticeButton(id, label) {
  const notice = doc.getElementById(id);
  const button = notice && notice.querySelectorAll('button').find((b) => b.textContent === label);
  if (!button) throw new Error('no "' + label + '" button in #' + id);
  button.dispatch('click', {});
}

function report(extra) {
  const lib = require(path.join(SCRIPTS, 'letter_placeholders.js'));
  process.stdout.write(
    JSON.stringify(
      Object.assign(
        {
          moved: page.movedThePerson,
          printNotice: noticeSnapshot(lib.PRINT_NOTICE_ID),
          faxNotice: noticeSnapshot(lib.FAX_NOTICE_ID),
          printNotices: count(lib.PRINT_NOTICE_ID),
          focused: describe(page.focused),
          selection: [letter.selectionStart, letter.selectionEnd],
          images: page.body.querySelectorAll('img').length,
        },
        extra || {},
      ),
    ),
  );
}

function loadReviewPage(text) {
  letter.value = text;
  require(path.join(SCRIPTS, 'escalation_packet_review.js'));
}

const scenarios = {
  find() {
    const {findUnfilledPlaceholders} = require(path.join(SCRIPTS, 'letter_placeholders.js'));
    const letters = JSON.parse(fs.readFileSync(0, 'utf8'));
    process.stdout.write(JSON.stringify({found: letters.map(findUnfilledPlaceholders)}));
  },
  'print-complete'() {
    loadReviewPage(COMPLETE);
    printButton.dispatch('click', {});
    report();
  },
  'print-blanks'() {
    loadReviewPage(WITH_BLANKS);
    printButton.dispatch('click', {});
    report();
  },
  'print-twice'() {
    loadReviewPage(WITH_BLANKS);
    printButton.dispatch('click', {});
    printButton.dispatch('click', {});
    report();
  },
  'print-anyway'() {
    loadReviewPage(WITH_BLANKS);
    printButton.dispatch('click', {});
    pressNoticeButton('print-placeholder-notice', 'Print anyway');
    report();
  },
  'print-show-me'() {
    loadReviewPage(WITH_BLANKS);
    printButton.dispatch('click', {});
    pressNoticeButton('print-placeholder-notice', 'Show me in the letter');
    report({firstBlankAt: WITH_BLANKS.indexOf('[Your Name]')});
  },
  'print-fixed'() {
    loadReviewPage(WITH_BLANKS);
    printButton.dispatch('click', {});
    letter.value = COMPLETE;
    printButton.dispatch('click', {});
    report();
  },
  'print-markup'() {
    loadReviewPage(WITH_MARKUP);
    printButton.dispatch('click', {});
    report();
  },
  'fax-blanks'() {
    const lib = require(path.join(SCRIPTS, 'letter_placeholders.js'));
    faxLetter.value = WITH_BLANKS;
    const waits = lib.faxMustWaitForPlaceholders(faxButton, faxLetter);
    report({waits});
  },
  'fax-fixed'() {
    const lib = require(path.join(SCRIPTS, 'letter_placeholders.js'));
    faxLetter.value = WITH_BLANKS;
    const first = lib.faxMustWaitForPlaceholders(faxButton, faxLetter);
    faxLetter.value = COMPLETE;
    const waits = lib.faxMustWaitForPlaceholders(faxButton, faxLetter);
    report({first, waits});
  },
};

const scenario = scenarios[scenarioName];
if (!scenario) throw new Error('unknown scenario ' + scenarioName);
scenario();
