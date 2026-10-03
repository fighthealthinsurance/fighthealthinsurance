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
// check the appeal page's fax form calls, from a submit handler shaped like
// appeal.ts's. The "find" scenario reads a JSON list of letters on stdin and
// prints what the browser finds in each, as the notice names the blanks and
// as the letter has them.
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
// The first blank's letters also sit, earlier, inside a karyotype the check
// leaves alone.
const KARYOTYPE_FIRST =
  'Dear Example Health,\n\nI have triple X syndrome (47,XXX).\nMember ID XXX\n\nSincerely,\nPat Example';
// The same letter after edits: one adds a blank it did not have, the other
// adds words and no blank.
const WITH_A_NEW_BLANK = WITH_BLANKS.replace('{{SCSID}}.', '{{SCSID}}, seen on [Date of Service].');
const WITH_THE_SAME_BLANKS = WITH_BLANKS.replace('{{SCSID}}.', '{{SCSID}}, a member for ten years.');
// The same letter after an edit that fills in both blanks and adds another.
const WITH_ONLY_A_DIFFERENT_BLANK =
  'Dear Example Health,\n\nI am Pat Example, member 12345, seen on [Date of Service].\n\nSincerely,\nPat Example';
const SIGNATURE_LINE = '______________';
const WITH_A_LINE = 'Dear Example Health,\n\nI am Pat Example.\n\nSigned: ' + SIGNATURE_LINE + '\nPat Example';
// The same letter with a second, shorter line to write on, under the first.
const DATE_LINE = '________';
const WITH_TWO_LINES = WITH_A_LINE.replace('\nPat Example', '\nDated: ' + DATE_LINE + '\nPat Example');

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
const faxForm = doc.getElementById('fax-form');
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

// What the fax form would post besides the letter: each named input, with
// a tick box only while it is ticked, as the list of values posted under
// each name. A list of approved blanks is read as the server reads it.
function posted() {
  const fields = {};
  for (const input of faxForm.querySelectorAll('input')) {
    if (input.getAttribute('type') === 'checkbox' && !input.checked) continue;
    const name = input.getAttribute('name');
    const value = input.getAttribute('value');
    (fields[name] = fields[name] || []).push(
      name === 'approved_placeholders' ? JSON.parse(value) : value,
    );
  }
  return fields;
}

// The fax form wired the way appeal.ts wires it: each submission asks the
// check, and one it holds is stopped. Pressing the fax button submits the
// form before click() returns, the way a browser does, and each submission
// is recorded as "held" or as what it would post.
function wireFaxForm(lib) {
  const submissions = [];
  faxForm.addEventListener('submit', (event) => {
    if (lib.faxMustWaitForPlaceholders(faxForm, faxButton, faxLetter)) {
      event.preventDefault();
    }
  });
  faxButton.click = () => {
    let held = false;
    faxForm.dispatch('submit', {preventDefault: () => (held = true)});
    submissions.push(held ? 'held' : posted());
  };
  return submissions;
}

function loadReviewPage(text) {
  letter.value = text;
  require(path.join(SCRIPTS, 'escalation_packet_review.js'));
}

const scenarios = {
  find() {
    const {findUnfilledPlaceholders} = require(path.join(SCRIPTS, 'letter_placeholders.js'));
    const letters = JSON.parse(fs.readFileSync(0, 'utf8'));
    const {findPlaceholdersAsWritten} = require(path.join(SCRIPTS, 'letter_placeholders.js'));
    process.stdout.write(
      JSON.stringify({
        found: letters.map(findUnfilledPlaceholders),
        asWritten: letters.map(findPlaceholdersAsWritten),
      }),
    );
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
  'print-show-me-past-a-karyotype'() {
    loadReviewPage(KARYOTYPE_FIRST);
    printButton.dispatch('click', {});
    pressNoticeButton('print-placeholder-notice', 'Show me in the letter');
    report({firstBlankAt: KARYOTYPE_FIRST.indexOf('ID XXX') + 'ID '.length});
  },
  'print-show-me-a-line'() {
    loadReviewPage(WITH_A_LINE);
    printButton.dispatch('click', {});
    pressNoticeButton('print-placeholder-notice', 'Show me in the letter');
    report({firstBlankAt: WITH_A_LINE.indexOf(SIGNATURE_LINE), lineLength: SIGNATURE_LINE.length});
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
    const waits = lib.faxMustWaitForPlaceholders(faxForm, faxButton, faxLetter);
    report({waits});
  },
  'fax-fixed'() {
    const lib = require(path.join(SCRIPTS, 'letter_placeholders.js'));
    faxLetter.value = WITH_BLANKS;
    const first = lib.faxMustWaitForPlaceholders(faxForm, faxButton, faxLetter);
    faxLetter.value = COMPLETE;
    const waits = lib.faxMustWaitForPlaceholders(faxForm, faxButton, faxLetter);
    report({first, waits});
  },
  'fax-send-anyway'() {
    const lib = require(path.join(SCRIPTS, 'letter_placeholders.js'));
    const submissions = wireFaxForm(lib);
    faxLetter.value = WITH_BLANKS;
    faxButton.click();
    pressNoticeButton(lib.FAX_NOTICE_ID, 'Send anyway');
    report({submissions});
  },
  'fax-send-anyway-then-again'() {
    const lib = require(path.join(SCRIPTS, 'letter_placeholders.js'));
    const submissions = wireFaxForm(lib);
    faxLetter.value = WITH_BLANKS;
    faxButton.click();
    pressNoticeButton(lib.FAX_NOTICE_ID, 'Send anyway');
    // Back on the page (the browser's back button), the letter unchanged.
    faxButton.click();
    report({submissions, leftOnTheForm: posted()});
  },
  'fax-send-anyway-then-a-new-blank'() {
    const lib = require(path.join(SCRIPTS, 'letter_placeholders.js'));
    const submissions = wireFaxForm(lib);
    faxLetter.value = WITH_BLANKS;
    faxButton.click();
    pressNoticeButton(lib.FAX_NOTICE_ID, 'Send anyway');
    // Back on the page, the person types in a blank the notice never listed.
    faxLetter.value = WITH_A_NEW_BLANK;
    faxButton.click();
    report({submissions, leftOnTheForm: posted()});
  },
  'fax-send-anyway-then-a-new-blank-sent-anyway'() {
    const lib = require(path.join(SCRIPTS, 'letter_placeholders.js'));
    const submissions = wireFaxForm(lib);
    faxLetter.value = WITH_BLANKS;
    faxButton.click();
    pressNoticeButton(lib.FAX_NOTICE_ID, 'Send anyway');
    faxLetter.value = WITH_A_NEW_BLANK;
    faxButton.click();
    pressNoticeButton(lib.FAX_NOTICE_ID, 'Send anyway');
    report({submissions});
  },
  'fax-send-anyway-then-only-a-different-blank'() {
    const lib = require(path.join(SCRIPTS, 'letter_placeholders.js'));
    const submissions = wireFaxForm(lib);
    faxLetter.value = WITH_BLANKS;
    faxButton.click();
    pressNoticeButton(lib.FAX_NOTICE_ID, 'Send anyway');
    // Back on the page, both blanks filled in and another typed in.
    faxLetter.value = WITH_ONLY_A_DIFFERENT_BLANK;
    faxButton.click();
    report({submissions});
  },
  'fax-send-anyway-then-an-edit'() {
    const lib = require(path.join(SCRIPTS, 'letter_placeholders.js'));
    const submissions = wireFaxForm(lib);
    faxLetter.value = WITH_BLANKS;
    faxButton.click();
    pressNoticeButton(lib.FAX_NOTICE_ID, 'Send anyway');
    // Back on the page, the person adds words and no blank.
    faxLetter.value = WITH_THE_SAME_BLANKS;
    faxButton.click();
    report({submissions});
  },
  'fax-new-blank-typed-before-send-anyway'() {
    const lib = require(path.join(SCRIPTS, 'letter_placeholders.js'));
    const submissions = wireFaxForm(lib);
    faxLetter.value = WITH_BLANKS;
    faxButton.click();
    // With the notice still showing, a blank it does not list is typed in.
    faxLetter.value = WITH_A_NEW_BLANK;
    pressNoticeButton(lib.FAX_NOTICE_ID, 'Send anyway');
    report({submissions});
  },
  'fax-send-anyway-then-a-new-blank-show-me'() {
    const lib = require(path.join(SCRIPTS, 'letter_placeholders.js'));
    const submissions = wireFaxForm(lib);
    faxLetter.value = WITH_BLANKS;
    faxButton.click();
    pressNoticeButton(lib.FAX_NOTICE_ID, 'Send anyway');
    faxLetter.value = WITH_A_NEW_BLANK;
    faxButton.click();
    pressNoticeButton(lib.FAX_NOTICE_ID, 'Show me in the letter');
    report({
      submissions,
      newBlankAt: WITH_A_NEW_BLANK.indexOf('[Date of Service]'),
      faxFocused: page.focused === faxLetter,
      faxSelection: [faxLetter.selectionStart, faxLetter.selectionEnd],
    });
  },
  'fax-show-me'() {
    const lib = require(path.join(SCRIPTS, 'letter_placeholders.js'));
    wireFaxForm(lib);
    faxLetter.value = WITH_BLANKS;
    faxButton.click();
    pressNoticeButton(lib.FAX_NOTICE_ID, 'Show me in the letter');
    report({
      firstBlankAt: WITH_BLANKS.indexOf('[Your Name]'),
      faxSelection: [faxLetter.selectionStart, faxLetter.selectionEnd],
    });
  },
  'fax-send-anyway-then-a-new-line'() {
    const lib = require(path.join(SCRIPTS, 'letter_placeholders.js'));
    const submissions = wireFaxForm(lib);
    faxLetter.value = WITH_A_LINE;
    faxButton.click();
    pressNoticeButton(lib.FAX_NOTICE_ID, 'Send anyway');
    // Back on the page, a second line to write on, of another length.
    faxLetter.value = WITH_TWO_LINES;
    faxButton.click();
    pressNoticeButton(lib.FAX_NOTICE_ID, 'Show me in the letter');
    report({
      submissions,
      // Not indexOf(DATE_LINE): those underscores start inside the longer line.
      newLineAt: WITH_TWO_LINES.indexOf('Dated: ' + DATE_LINE) + 'Dated: '.length,
      newLineLength: DATE_LINE.length,
      faxSelection: [faxLetter.selectionStart, faxLetter.selectionEnd],
    });
  },
  'fax-send-anyway-then-the-same-line'() {
    const lib = require(path.join(SCRIPTS, 'letter_placeholders.js'));
    const submissions = wireFaxForm(lib);
    faxLetter.value = WITH_A_LINE;
    faxButton.click();
    pressNoticeButton(lib.FAX_NOTICE_ID, 'Send anyway');
    // Back on the page, words added above the line, which is unchanged.
    faxLetter.value = WITH_A_LINE.replace('I am Pat Example.', 'I am Pat Example, a member.');
    faxButton.click();
    report({submissions});
  },
  'fax-box-ticked'() {
    const lib = require(path.join(SCRIPTS, 'letter_placeholders.js'));
    const submissions = wireFaxForm(lib);
    addSendItAsItIsBox(true, ['[Your Name]', '{{SCSID}}']);
    faxLetter.value = WITH_BLANKS;
    faxButton.click();
    report({submissions});
  },
  'fax-box-unticked'() {
    const lib = require(path.join(SCRIPTS, 'letter_placeholders.js'));
    const submissions = wireFaxForm(lib);
    addSendItAsItIsBox(false, ['[Your Name]', '{{SCSID}}']);
    faxLetter.value = WITH_BLANKS;
    faxButton.click();
    report({submissions});
  },
  'fax-box-ticked-without-a-list'() {
    const lib = require(path.join(SCRIPTS, 'letter_placeholders.js'));
    const submissions = wireFaxForm(lib);
    addSendItAsItIsBox(true, null);
    faxLetter.value = WITH_BLANKS;
    faxButton.click();
    report({submissions});
  },
  'fax-box-ticked-then-only-a-different-blank'() {
    const lib = require(path.join(SCRIPTS, 'letter_placeholders.js'));
    const submissions = wireFaxForm(lib);
    addSendItAsItIsBox(true, ['[Your Name]', '{{SCSID}}']);
    // The page came back naming two blanks; both are filled in and another
    // is typed in.
    faxLetter.value = WITH_ONLY_A_DIFFERENT_BLANK;
    faxButton.click();
    report({submissions});
  },
  'fax-box-ticked-then-a-new-blank'() {
    const lib = require(path.join(SCRIPTS, 'letter_placeholders.js'));
    const submissions = wireFaxForm(lib);
    addSendItAsItIsBox(true, ['[Your Name]', '{{SCSID}}']);
    // The page came back naming two blanks; a third is typed in.
    faxLetter.value = WITH_A_NEW_BLANK;
    faxButton.click();
    pressNoticeButton(lib.FAX_NOTICE_ID, 'Show me in the letter');
    report({
      submissions,
      newBlankAt: WITH_A_NEW_BLANK.indexOf('[Date of Service]'),
      faxSelection: [faxLetter.selectionStart, faxLetter.selectionEnd],
    });
  },
};

// The tick box the server's page puts under the letter, its value the list
// of blanks that page names (null: a box with no list, valued "1").
function addSendItAsItIsBox(ticked, blanks) {
  const box = doc.createElement('input');
  box.id = 'id_approved_placeholders';
  box.setAttribute('type', 'checkbox');
  box.setAttribute('name', 'approved_placeholders');
  box.setAttribute('value', blanks === null ? '1' : JSON.stringify(blanks));
  box.checked = ticked;
  faxForm.insertBefore(box, faxButton);
}

const scenario = scenarios[scenarioName];
if (!scenario) throw new Error('unknown scenario ' + scenarioName);
scenario();
