'use strict';
// Drive the real compiled entity_fetcher over a fake page and report what the
// person would be looking at. One scenario per process: the module keeps run
// state at module scope, so a fresh process is the only way to be sure a
// scenario is not reading the leftovers of the one before it.
//
//   node entity_fetcher_behaviour.cjs <compiled entity_fetcher.js> <scenario>
//
// Writes one JSON object to stdout; everything the page logs is swallowed.

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

// What a person could read off the page: a block with display:none on it is
// not on the screen, whatever the tree still holds.
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
  good_run() {
    const ws = start();
    ws.fireOpen();
    const opening = JSON.parse(ws.sent[0]);
    ws.fireMessage(STEP_FOUND);
    ws.fireMessage(RUN_FINISHED);
    const ended = snapshot();
    ws.fireClose();
    page.clock.advance(5000);
    return {opening, ended, afterTheSocketClosed: snapshot()};
  },

  // The opposite news on the same wire.
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

  // A socket that closes having sent nothing.
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

  inactivity_timeout() {
    const ws = start();
    ws.fireOpen();
    ws.fireMessage(STEP_FOUND);
    const beforeTheTimeout = snapshot();
    page.clock.advance(60000);
    return {beforeTheTimeout, ended: snapshot()};
  },

  // A frame the server gave no words for is an internal step name.
  unlabeled_step_never_renders() {
    const ws = start();
    ws.fireOpen();
    ws.fireMessage({type: 'step', task: 'plan_document_summary', outcome: 'found'});
    ws.fireMessage({type: 'step', task: 'extract_set_triage', outcome: 'found'});
    ws.fireMessage(STEP_FOUND);
    ws.fireMessage(RUN_FINISHED);
    return {ended: snapshot()};
  },

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

  // close() asks the browser to hang up; anything already queued on the wire
  // is still delivered, and ``settled`` went back to false when the new run
  // started, so nothing but the generation would stop it.
  stale_frame_lands_during_the_next_run() {
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
    // The old socket speaks: a step, then the verdict of a run already over.
    ws.fireMessage({
      type: 'step',
      task: 'extract_set_plan_id',
      outcome: 'found',
      label: 'Plan ID',
    });
    ws.fireMessage(RUN_FINISHED);
    const afterTheStaleFrames = snapshot();
    second.fireMessage(STEP_FOUND);
    second.fireMessage(RUN_FOUND_NOTHING);
    return {afterTheStaleFrames, ended: snapshot()};
  },

  // A reconnect booked by a run the person then abandons by pressing retry.
  reconnect_from_the_previous_run_never_opens() {
    const ws = start();
    // Never opens, never says anything, hangs up just under the minute, which
    // books a reconnect a second later.
    page.clock.advance(58500);
    ws.fireClose();
    page.clock.advance(1000);
    const second = page.sockets[page.sockets.length - 1];
    second.fireClose();
    // The inactivity timer runs from the start of the run, not from the last
    // socket, so it comes due while that reconnect is still booked.
    page.clock.advance(500);
    const ended = snapshot();
    const actions = page.document.getElementById('entity-status-actions');
    const retry = actions.childNodes.find(
      (n) => n.nodeType === 1 && n.textContent.includes('Try reading'),
    );
    retry.dispatch('click');
    const third = page.sockets[page.sockets.length - 1];
    third.fireOpen();
    page.clock.advance(5000);
    const afterTheOldReconnect = snapshot();
    third.fireMessage(STEP_FOUND);
    third.fireMessage(RUN_FINISHED);
    return {
      ended,
      afterTheOldReconnect,
      finished: snapshot(),
      // finish() hangs up whatever is in ``activeSocket``, so a stale socket
      // that has taken that slot leaves the live run's socket open.
      hungUpOn: page.sockets.map((s) => s.closedByPage),
    };
  },

  // Nobody presses anything: the run ends by itself and the reconnect falls
  // due after the verdict is already on the screen.
  reconnect_after_the_verdict_never_opens() {
    const ws = start();
    // Hangs up at 59.5s, booking a reconnect for 60.5s.
    page.clock.advance(59500);
    ws.fireClose();
    const beforeTheTimeout = snapshot();
    // 60s: the inactivity timer paints the terminal state.
    page.clock.advance(500);
    const ended = snapshot();
    // 60.5s: the booked reconnect falls due.
    page.clock.advance(5000);
    return {beforeTheTimeout, ended, afterTheBookedReconnect: snapshot()};
  },

  // The close event for the run that just ended arrives while the run the
  // person started by pressing retry is already in flight.
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
