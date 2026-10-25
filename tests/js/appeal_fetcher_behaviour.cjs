'use strict';
// Drive the real compiled appeal_fetcher over a fake appeals page and report
// what the person would be looking at. One scenario per process, because the
// module keeps its run state at module scope.
//
//   node appeal_fetcher_behaviour.cjs <compiled appeal_fetcher.js> <scenario>
//
// Writes one JSON object to stdout; everything the page logs is swallowed.

const path = require('path');
const {buildPage, install, makeJquery, APPEALS_MARKUP} = require(
  path.join(__dirname, 'fake_page.cjs'),
);

const [, , modulePath, scenarioName] = process.argv;
if (!modulePath || !scenarioName) {
  throw new Error('usage: appeal_fetcher_behaviour.cjs <module> <scenario>');
}

const WS_URL = 'wss://example.test/ws/streaming-appeals-backend/';
const REST_URL = 'https://example.test/ziggy/rest/streaming_appeals_fallback';
const ENABLE_URL = 'https://example.test/enable_external_models';
const DATA = {
  denial_id: '7',
  email: 'rebecca.crumpler@example.test',
  semi_sekret: 'sekret',
  csrfmiddlewaretoken: 'token',
};
const LETTER_A = 'Dear reviewer, the MRI was ordered by Dr. Rebecca Lee Crumpler.';
const LETTER_B = 'Dear reviewer, the plan covers this imaging.';
const LETTER_C = 'Dear reviewer, the guidelines support this imaging.';
const EDITED = 'Dear reviewer, I added my own paragraph to this one.';

const page = buildPage(APPEALS_MARKUP);
install(page);
global.$ = makeJquery(page);
global.window.enableExternalModelsUrl = ENABLE_URL;
for (const level of ['debug', 'log', 'info', 'warn', 'error']) {
  console[level] = () => {};
}

// Every request the page makes is recorded and none leaves the process. The
// backup REST request never gets a response; the external-models opt-in
// waits until a scenario answers it.
page.fetches = [];
let enableRequest = null;
global.fetch = (url, options) => {
  page.fetches.push(String(url));
  if (String(url) === ENABLE_URL) {
    return new Promise((resolve, reject) => {
      enableRequest = {resolve, reject};
    });
  }
  if (String(url).indexOf('report_client_error') >= 0) {
    return Promise.resolve({ok: true});
  }
  return Promise.reject(new TypeError('Failed to fetch'));
};

const fetcher = require(path.resolve(modulePath));

function letter(text, id) {
  return JSON.stringify({content: text, id: id}) + '\n';
}

const SERVER_FAILED =
  JSON.stringify({type: 'error', message: 'Appeal generation failed on the server. Please retry.'}) +
  '\n';

// The page's promise chains run on real microtasks; this lets them settle.
function flush() {
  return new Promise((resolve) => setImmediate(resolve));
}

function byId(id) {
  return page.document.getElementById(id);
}

function norm(text) {
  return String(text).replace(/\s+/g, ' ').trim();
}

function visibleText(node) {
  if (node.nodeType === 3) return node.data;
  if (node.style && node.style.display === 'none') return '';
  return (node.childNodes || []).map(visibleText).join(' ');
}

function lastSocket() {
  return page.sockets[page.sockets.length - 1];
}

function drafts() {
  return byId('output-container')
    .childNodes.filter((n) => n.nodeType === 1 && n.id.indexOf('magic') === 0)
    .map((el) => ({id: el.id, text: el.querySelector('textarea').value}));
}

function snapshot() {
  const panel = byId('appeal-retry-panel');
  const message = panel ? panel.querySelector('p') : null;
  const status = byId('status-message');
  return {
    panelMessage: message ? norm(message.textContent) : null,
    panelClass: panel ? panel.className : null,
    panelRole: panel ? panel.getAttribute('role') || null : null,
    panelLive: panel ? panel.getAttribute('aria-live') || null : null,
    panelSitsAboveTheDrafts: panel ? panel.nextSibling === byId('output-container') : null,
    buttons: panel
      ? panel.querySelectorAll('button').map((b) => ({text: norm(b.textContent), type: b.type}))
      : [],
    indicatorShown: !!byId('appeal-status-indicator'),
    statusMessage: status ? norm(status.textContent) : null,
    loadingTextDisplay: byId('loading-text').style.display || '',
    externalPromptDisplay: byId('external-models-prompt').style.display || '',
    drafts: drafts(),
    visibleText: norm(visibleText(page.document.body)),
    movedThePerson: page.movedThePerson.slice(),
    socketCount: page.sockets.length,
    errorReports: page.fetches.filter((u) => u.indexOf('report_client_error') >= 0).length,
    enableRequests: page.fetches.filter((u) => u === ENABLE_URL).length,
  };
}

function start(withRest) {
  fetcher.doQuery(WS_URL, Object.assign({}, DATA), withRest ? REST_URL : undefined);
  return lastSocket();
}

function pressRetry() {
  byId('appeal-retry').dispatch('click');
  return lastSocket();
}

// With no backup request configured: every socket errors, four reconnects
// are tried a second apart, and the fifth error is the end of the run.
function everySocketErrors() {
  for (let i = 0; i < 5; i++) {
    lastSocket().fireError();
    if (i < 4) page.clock.advance(1000);
  }
}

// A socket that keeps sending keep-alives until the seven-minute cap gives up
// on it. Its close event, booked by close(), lands closeMs after that: the
// closing handshake is a network round trip, and the retry button is on the
// screen for all of it.
function runUntilTheCapGivesUp(closeMs) {
  const ws = start(false);
  ws.close = function () {
    this.closedByPage = true;
    page.clock.setTimeout(() => this.fireClose(), closeMs);
  };
  ws.fireOpen();
  for (let i = 0; i < 6; i++) {
    page.clock.advance(60000);
    ws.fireMessage('\n');
  }
  page.clock.advance(60000);
  return ws;
}

function deliverThreeAndClose(ws) {
  ws.fireMessage(letter(LETTER_A, 101));
  ws.fireMessage(letter(LETTER_B, 102));
  ws.fireMessage(letter(LETTER_C, 103));
  ws.fireClose();
  page.clock.advance(1500);
}

const scenarios = {
  // Three letters and a clean close: nothing failed, nothing to retry.
  good_run() {
    const ws = start(true);
    ws.fireOpen();
    deliverThreeAndClose(ws);
    return {ended: snapshot()};
  },

  // The usual production failure: the socket goes quiet and the backup
  // request never gets a response.
  async rest_fallback_fails() {
    const ws = start(true);
    ws.fireOpen();
    page.clock.advance(90000);
    await flush();
    page.clock.advance(1500);
    return {ended: snapshot()};
  },

  // Every socket closes cleanly having written nothing.
  nothing_arrives() {
    start(false);
    for (let i = 0; i < 5; i++) {
      lastSocket().fireClose();
    }
    page.clock.advance(1500);
    return {ended: snapshot()};
  },

  // One letter, then the server says generation failed. The four automatic
  // attempts after it add nothing.
  server_reports_the_failure() {
    const ws = start(false);
    ws.fireOpen();
    ws.fireMessage(letter(LETTER_A, 101));
    ws.fireMessage(SERVER_FAILED);
    ws.fireClose();
    const betweenAttempts = snapshot();
    for (let i = 0; i < 4; i++) {
      lastSocket().fireClose();
    }
    page.clock.advance(1500);
    return {betweenAttempts, ended: snapshot()};
  },

  async retry_runs_again_in_place() {
    const ws = start(true);
    ws.fireOpen();
    page.clock.advance(90000);
    await flush();
    page.clock.advance(1500);
    const ended = snapshot();
    const second = pressRetry();
    second.fireOpen();
    const during = snapshot();
    const opening = JSON.parse(second.sent[0]);
    deliverThreeAndClose(second);
    return {ended, during, opening, finished: snapshot()};
  },

  // A letter arrived before the run failed. The person edits it, then
  // retries, and the server serves that letter again with two new ones.
  retry_keeps_the_drafts_that_arrived() {
    const ws = start(false);
    ws.fireOpen();
    ws.fireMessage(letter(LETTER_A, 101));
    everySocketErrors();
    page.clock.advance(1500);
    const ended = snapshot();
    const textarea = byId('magic1').querySelector('textarea');
    textarea.value = EDITED;
    textarea.dispatch('input');
    const second = pressRetry();
    second.fireOpen();
    const during = snapshot();
    deliverThreeAndClose(second);
    return {ended, during, finished: snapshot()};
  },

  // Two letters arrived before the run failed. The retry is served those two
  // again, and then the server says generation failed.
  retry_that_only_replays_the_kept_drafts_then_fails() {
    const ws = start(false);
    ws.fireOpen();
    ws.fireMessage(letter(LETTER_A, 101));
    ws.fireMessage(letter(LETTER_B, 102));
    everySocketErrors();
    const second = pressRetry();
    second.fireOpen();
    second.fireMessage(letter(LETTER_A, 101));
    second.fireMessage(letter(LETTER_B, 102));
    second.fireMessage(SERVER_FAILED);
    second.fireClose();
    page.clock.advance(1500);
    return {ended: snapshot()};
  },

  // The browser fires close after error. On the socket that ended the run,
  // that close must not be read as a second ending.
  the_failed_sockets_own_close_changes_nothing() {
    const ws = start(false);
    ws.fireOpen();
    everySocketErrors();
    page.clock.advance(1500);
    const ended = snapshot();
    lastSocket().fireClose();
    page.clock.advance(5000);
    const afterItsOwnClose = snapshot();
    return {ended, afterItsOwnClose};
  },

  second_press_starts_nothing() {
    const ws = start(false);
    ws.fireOpen();
    everySocketErrors();
    const button = byId('appeal-retry');
    button.dispatch('click');
    button.dispatch('click');
    const afterTwoPresses = snapshot();
    // Hidden while the run goes, and pressing it anyway starts nothing.
    byId('request-external-models-btn').dispatch('click');
    return {afterTwoPresses, afterTheOtherButton: snapshot()};
  },

  // The person asks for external models and presses retry while that
  // request is still out.
  async retry_waits_for_the_external_models_request() {
    const ws = start(false);
    ws.fireOpen();
    everySocketErrors();
    byId('request-external-models-btn').dispatch('click');
    byId('appeal-retry').dispatch('click');
    const whileTheRequestIsOut = snapshot();
    enableRequest.resolve({ok: true, status: 200, statusText: 'OK'});
    await flush();
    lastSocket().fireOpen();
    return {whileTheRequestIsOut, afterItCameBack: snapshot()};
  },

  // Two letters and a clean end, so the page says they are ready and books
  // its panel's removal three seconds out. The person opts into external
  // models inside those three seconds.
  async external_rerun_keeps_its_progress_panel() {
    const ws = start(false);
    ws.fireOpen();
    ws.fireMessage(letter(LETTER_A, 101));
    ws.fireMessage(letter(LETTER_B, 102));
    ws.fireClose();
    for (let i = 0; i < 4; i++) {
      lastSocket().fireClose();
    }
    const ended = snapshot();
    byId('request-external-models-btn').dispatch('click');
    enableRequest.resolve({ok: true, status: 200, statusText: 'OK'});
    await flush();
    lastSocket().fireOpen();
    page.clock.advance(5000);
    return {ended, duringTheRerun: snapshot()};
  },

  // The cap gives up, the person presses retry at once, and the given-up
  // socket's close event lands after that.
  stale_close_lands_during_the_retry() {
    runUntilTheCapGivesUp(250);
    const ended = snapshot();
    const second = pressRetry();
    second.fireOpen();
    page.clock.advance(1500);
    const afterTheOldClose = snapshot();
    deliverThreeAndClose(second);
    return {ended, afterTheOldClose, finished: snapshot()};
  },

  // Half a frame from the given-up socket, already on the wire, arrives
  // after the retry has started.
  stale_frame_lands_during_the_retry() {
    const old = runUntilTheCapGivesUp(5000);
    const second = pressRetry();
    second.fireOpen();
    old.fireMessage('{"content": "Half a letter from the run that gave');
    deliverThreeAndClose(second);
    return {finished: snapshot()};
  },

  // A socket closed while still connecting fires error, then close. Here the
  // error lands after the retry has started.
  stale_error_lands_during_the_retry() {
    const old = runUntilTheCapGivesUp(5000);
    const second = pressRetry();
    old.fireError();
    second.fireOpen();
    return {opening: JSON.parse(second.sent[0]), during: snapshot()};
  },

  // A socket errors just before the cap, booking a reconnect a second out;
  // the cap gives up, and the person presses retry before that second is up.
  reconnect_booked_by_the_failed_run_never_opens() {
    const ws = start(false);
    ws.fireOpen();
    for (let i = 0; i < 6; i++) {
      page.clock.advance(60000);
      ws.fireMessage('\n');
    }
    page.clock.advance(59500);
    ws.fireError();
    ws.fireClose();
    page.clock.advance(600);
    const ended = snapshot();
    const second = pressRetry();
    second.fireOpen();
    page.clock.advance(1000);
    return {ended, afterTheOldReconnect: snapshot()};
  },
};

const scenario = scenarios[scenarioName];
if (!scenario) {
  throw new Error('unknown scenario ' + scenarioName + '; have ' + Object.keys(scenarios).join(', '));
}
Promise.resolve(scenario()).then((result) => {
  process.stdout.write(JSON.stringify(result, null, 2));
});
