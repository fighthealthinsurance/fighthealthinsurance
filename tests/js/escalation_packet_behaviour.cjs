'use strict';
// Drive the escalation page's own script over a fake page and report what the
// person would be looking at. One scenario per process, for the same reason
// the extraction driver works that way: the script keeps its state in the
// closure it builds on DOMContentLoaded, and a fresh process is the only way
// to be sure a scenario is not reading the leftovers of the one before it.
//
//   node escalation_packet_behaviour.cjs <script extracted from the template> <scenario>
//
// The script comes from rendering escalation_packet.html, not from a copy kept
// here: a fixture that drifts from the template tests nothing.
//
// Writes one JSON object to stdout. Everything the page logs is swallowed so
// the only thing on stdout is that object.

const fs = require('fs');
const path = require('path');
const {buildPage, install, ESCALATION_MARKUP} = require(path.join(__dirname, 'fake_page.cjs'));

const [, , scriptPath, scenarioName] = process.argv;
if (!scriptPath || !scenarioName) {
  throw new Error('usage: escalation_packet_behaviour.cjs <script> <scenario>');
}

const page = buildPage(ESCALATION_MARKUP);
install(page);
for (const level of ['debug', 'log', 'info', 'warn', 'error']) {
  console[level] = () => {};
}

function letter(name, id) {
  return {
    type: 'letter',
    recipient_name: name,
    recipient_address: '1 Regulator Way',
    recipient_phone: '555-0100',
    recipient_url: 'https://example.test/' + id,
    rationale: 'They regulate this plan.',
    escalation_id: id,
    content: 'Dear ' + name + ', ...',
  };
}

function textOf(el) {
  return el ? el.textContent.replace(/\s+/g, ' ').trim() : null;
}

// What a person could actually read off the page. display:none is off the
// screen, which is the whole question here: a loading block that says
// "Drafting your regulator letters..." is a lie once the socket is gone, and
// hiding it is the other lie.
function visibleText(node) {
  if (node.nodeType === 3) return node.data;
  if (node.style && node.style.display === 'none') return '';
  return (node.childNodes || []).map(visibleText).join('');
}

function snapshot() {
  const loading = page.document.getElementById('loading-text');
  const letters = page.document.getElementById('escalation-letters');
  const rendered = letters
    ? letters.childNodes.filter((n) => n.nodeType === 1)
    : [];
  return {
    loadingDisplay: loading ? loading.style.display || '' : null,
    loadingHeading: textOf(loading && loading.querySelector('h4')),
    loadingDetail: textOf(loading && loading.querySelector('p')),
    loadingBorderLeft: loading ? loading.style.borderLeft || '' : null,
    letterCount: rendered.length,
    letterHeadings: rendered.map((w) => textOf(w.querySelector('h4'))),
    visibleText: visibleText(page.document.body).replace(/\s+/g, ' ').trim(),
    movedThePerson: page.movedThePerson.slice(),
  };
}

function start() {
  const source = fs.readFileSync(path.resolve(scriptPath), 'utf8');
  // eslint-disable-next-line no-new-func
  new Function(source)();
  page.fireDomReady();
  return page.sockets[page.sockets.length - 1];
}

function send(ws, frames) {
  ws.fireMessage(frames.map((f) => JSON.stringify(f)).join('\n') + '\n');
}

const scenarios = {
  // The defect this branch exists for, on this page: the socket dies after two
  // of four letters, and closing was read as finishing.
  close_without_done() {
    const ws = start();
    ws.fireOpen();
    send(ws, [letter('State Insurance Commissioner', 'a'), letter('State AG', 'b')]);
    const beforeTheClose = snapshot();
    ws.fireClose();
    return {beforeTheClose, ended: snapshot()};
  },

  // The one case where hiding the block is honest: the server says every
  // letter is there.
  done_complete() {
    const ws = start();
    ws.fireOpen();
    send(ws, [
      letter('State Insurance Commissioner', 'a'),
      letter('State AG', 'b'),
      {type: 'status', phase: 'done', total: 2, generated: 2, cached: 0, complete: true},
    ]);
    const beforeTheClose = snapshot();
    ws.fireClose();
    return {beforeTheClose, ended: snapshot()};
  },

  // A done frame that is not a complete one. The server skipped a recipient it
  // could not draft for and carried on, so the packet is short.
  done_incomplete() {
    const ws = start();
    ws.fireOpen();
    send(ws, [
      letter('State Insurance Commissioner', 'a'),
      letter('State AG', 'b'),
      {
        type: 'status',
        phase: 'done',
        total: 4,
        generated: 1,
        cached: 1,
        complete: false,
        failed_names: ['Federal Ombudsman', 'Plan Appeals Board'],
      },
    ]);
    const beforeTheClose = snapshot();
    ws.fireClose();
    return {beforeTheClose, ended: snapshot()};
  },

  // onerror lands, then the socket closes a moment later. The close used to
  // erase what onerror had just written.
  error_then_close() {
    const ws = start();
    ws.fireOpen();
    send(ws, [letter('State Insurance Commissioner', 'a')]);
    ws.fireError();
    const afterTheError = snapshot();
    ws.fireClose();
    return {afterTheError, ended: snapshot()};
  },

  // A progress message arriving after bad news must not paint over it.
  status_cannot_paint_over_a_failure() {
    const ws = start();
    ws.fireOpen();
    send(ws, [{type: 'error', message: 'We could not reach the letter writer.'}]);
    const afterTheError = snapshot();
    send(ws, [{type: 'status', phase: 'generating', message: 'Drafting letter 2 of 4...'}]);
    const afterTheStatus = snapshot();
    ws.fireClose();
    return {afterTheError, afterTheStatus, ended: snapshot()};
  },

  // A socket that closes having said nothing at all, which is what an
  // already-done early exit on the server looks like from here.
  close_with_nothing_at_all() {
    const ws = start();
    ws.fireOpen();
    ws.fireClose();
    return {ended: snapshot()};
  },
};

const scenario = scenarios[scenarioName];
if (!scenario) {
  throw new Error(
    'unknown scenario ' + scenarioName + '; have ' + Object.keys(scenarios).join(', '),
  );
}
process.stdout.write(JSON.stringify(scenario(), null, 2));
