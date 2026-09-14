'use strict';
// Drive the real compiled entity_fetcher over a fake page and report what the
// person would be looking at. One scenario per process: the module keeps run
// state at module scope (``settled``, the timers, the socket), and a fresh
// process is the only way to be sure a scenario is not reading the leftovers
// of the one before it.
//
//   node entity_fetcher_behaviour.cjs <compiled entity_fetcher.js> <scenario>
//
// Writes one JSON object to stdout. Everything the page logs is swallowed so
// the only thing on stdout is that object.

const path = require('path');
const {buildPage, install} = require(path.join(__dirname, 'fake_page.cjs'));

const [, , modulePath, scenarioName] = process.argv;
if (!modulePath || !scenarioName) {
  throw new Error('usage: entity_fetcher_behaviour.cjs <module> <scenario>');
}

const page = buildPage();
install(page);
for (const level of ['debug', 'log', 'info', 'warn', 'error']) {
  console[level] = () => {};
}

const fetcher = require(path.resolve(modulePath));

const STEP_FOUND = {
  type: 'step',
  task: 'extract_set_fax_number',
  outcome: 'found',
  label: 'Fax number',
};
const RUN_FINISHED = {
  type: 'run',
  task: 'run',
  outcome: 'run_finished',
  label: 'We read your letter and filled in what we found. Check it on the next page.',
};
const RUN_FOUND_NOTHING = {
  type: 'run',
  task: 'run',
  outcome: 'run_read_and_found_nothing',
  label:
    'We read your letter and did not find the procedure or the diagnosis in it. ' +
    'You can have us try again, or type them in yourself.',
};

function textOf(id) {
  const el = page.document.getElementById(id);
  return el ? el.textContent.replace(/\s+/g, ' ').trim() : null;
}

function buttons() {
  const actions = page.document.getElementById('entity-status-actions');
  if (!actions) return [];
  return actions.childNodes
    .filter((n) => n.nodeType === 1 && n.tagName === 'BUTTON')
    .map((b) => ({
      text: b.textContent.trim(),
      type: b.type,
      className: b.className,
    }));
}

// What a person could actually read off the page. A block with display:none
// on it is not on the screen, which is the whole point of hiding the yellow
// "Analyzing your denial..." spinner when the run ends: the test for "the page
// does not say two things at once" has to look at what is visible, not at what
// is still in the tree.
function visibleText(node) {
  if (node.nodeType === 3) return node.data;
  if (node.style && node.style.display === 'none') return '';
  return (node.childNodes || []).map(visibleText).join('');
}

function snapshot() {
  const waiting = page.document.getElementById('waiting-msg');
  const indicator = page.document.getElementById('entity-status-indicator');
  const actions = page.document.getElementById('entity-status-actions');
  return {
    waitingDisplay: waiting ? waiting.style.display || '' : null,
    waitingStillSaysAnalyzing: waiting
      ? waiting.textContent.toLowerCase().includes('analyzing your denial')
      : null,
    title: textOf('entity-status-title'),
    steps: textOf('entity-status-list'),
    timer: textOf('entity-timer'),
    borderColor: indicator ? indicator.style.borderColor || '' : null,
    actionsDisplay: actions ? actions.style.display || '' : null,
    buttons: buttons(),
    visibleText: visibleText(page.document.body).replace(/\s+/g, ' ').trim(),
    movedThePerson: page.movedThePerson.slice(),
    socketCount: page.sockets.length,
  };
}

function start() {
  fetcher.doQuery('wss://example.test/ws/streaming-entity-backend/', {denial_id: 7}, 0);
  return page.sockets[page.sockets.length - 1];
}

const scenarios = {
  // A run that goes well. The green button under it must not be offering to
  // let the person type in what we just filled in, and the yellow block must
  // not still be saying we are reading the letter.
  good_run() {
    const ws = start();
    ws.fireOpen();
    const opening = JSON.parse(ws.sent[0]);
    ws.fireMessage(STEP_FOUND);
    ws.fireMessage(RUN_FINISHED);
    const ended = snapshot();
    // The socket hanging up after a finished run is not a second answer.
    ws.fireClose();
    page.clock.advance(5000);
    return {opening, ended, afterTheSocketClosed: snapshot()};
  },

  // The opposite news on the same wire. Both ways out, and the words that go
  // with a run that found nothing.
  nothing_found() {
    const ws = start();
    ws.fireOpen();
    ws.fireMessage({
      type: 'step',
      task: 'extract_set_denial_and_diagnosis',
      outcome: 'nothing_found',
      label: 'Procedure and diagnosis',
    });
    ws.fireMessage(RUN_FOUND_NOTHING);
    return {ended: snapshot()};
  },

  // A socket that closes having sent nothing. This is the run the page used to
  // paint green and click Next on.
  dead_socket() {
    start();
    for (let i = 0; i < 4; i++) {
      const ws = page.sockets[page.sockets.length - 1];
      if (ws.closeFired) break;
      ws.fireClose();
      page.clock.advance(1500);
    }
    return {ended: snapshot()};
  },

  // A run that opens, says one thing and then goes quiet forever.
  inactivity_timeout() {
    const ws = start();
    ws.fireOpen();
    ws.fireMessage(STEP_FOUND);
    const beforeTheTimeout = snapshot();
    page.clock.advance(60000);
    return {beforeTheTimeout, ended: snapshot()};
  },

  // A frame the server gave no words for is an internal step name. It is not
  // for the person and must not reach the page under any spelling.
  unlabeled_step_never_renders() {
    const ws = start();
    ws.fireOpen();
    ws.fireMessage({type: 'step', task: 'plan_document_summary', outcome: 'found'});
    ws.fireMessage({type: 'step', task: 'extract_set_triage', outcome: 'found'});
    ws.fireMessage(STEP_FOUND);
    ws.fireMessage(RUN_FINISHED);
    return {ended: snapshot()};
  },

  // A reconnect scheduled in the second before a timer fired can deliver
  // frames after the page has already given its answer.
  late_frame_cannot_repaint() {
    const ws = start();
    ws.fireOpen();
    ws.fireMessage(STEP_FOUND);
    ws.fireMessage(RUN_FINISHED);
    const ended = snapshot();
    ws.fireMessage({
      type: 'step',
      task: 'extract_set_plan_id',
      outcome: 'found',
      label: 'Plan ID',
    });
    ws.fireMessage({
      type: 'run',
      task: 'run',
      outcome: 'run_failed',
      label: 'We could not finish reading your letter.',
    });
    return {ended, afterTheLateFrames: snapshot()};
  },

  // The retry control is a control, not a reload: pressing it runs again in
  // place and tells the server this read is an authorized retry.
  retry_button_runs_again() {
    const ws = start();
    ws.fireOpen();
    ws.fireMessage(RUN_FOUND_NOTHING);
    const actions = page.document.getElementById('entity-status-actions');
    const retry = actions.childNodes.find(
      (n) => n.nodeType === 1 && n.textContent.includes('Try reading'),
    );
    retry.dispatch('click');
    const second = page.sockets[page.sockets.length - 1];
    second.fireOpen();
    return {
      duringTheSecondRun: snapshot(),
      secondOpening: JSON.parse(second.sent[0]),
      socketCount: page.sockets.length,
    };
  },

  // The close event for the run that just ended arrives while the run the
  // person started by pressing retry is already in flight. A browser delivers
  // it after the closing handshake, which is a network round trip, and the
  // retry button is on the screen for the whole of that window.
  close_event_lands_during_the_next_run() {
    const ws = start();
    ws.fireOpen();
    ws.fireMessage(RUN_FOUND_NOTHING);
    const actions = page.document.getElementById('entity-status-actions');
    const retry = actions.childNodes.find(
      (n) => n.nodeType === 1 && n.textContent.includes('Try reading'),
    );
    retry.dispatch('click');
    const second = page.sockets[page.sockets.length - 1];
    second.fireOpen();
    // Now the first socket's close finally lands.
    page.clock.advance(10);
    const afterTheOldClose = snapshot();
    // And the second run goes on to finish normally.
    second.fireMessage(STEP_FOUND);
    second.fireMessage(RUN_FINISHED);
    return {afterTheOldClose, ended: snapshot(), socketCount: page.sockets.length};
  },
};

const scenario = scenarios[scenarioName];
if (!scenario) {
  throw new Error('unknown scenario ' + scenarioName + '; have ' + Object.keys(scenarios).join(', '));
}
process.stdout.write(JSON.stringify(scenario(), null, 2));
