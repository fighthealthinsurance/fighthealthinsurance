declare const $: any;
import * as Sentry from '@sentry/browser';

// Every frame the extraction socket sends is a JSON object carrying a task and
// an outcome. `label` is present only for the steps that have words meant for
// the person reading the page; a frame without one is never rendered, which is
// how internal step names stay off the page without a fallback that prints
// them.
interface ExtractionFrame {
  type?: string;
  task?: string;
  outcome?: string;
  label?: string;
  message?: string;
}

// A run that goes quiet is a run that failed. The socket closing used to be
// read as success, so a dead connection painted a green all-done banner and
// clicked Next; now silence lands on the could-not-read state instead.
const INACTIVITY_MS = 60000;
const HARD_CAP_MS = 120000;
// Transient connection blips are worth one or two reconnects, but only while
// nothing at all has arrived.
const MAX_CONNECT_RETRIES = 2;

// The client's own words for the states no server frame can describe, because
// the server never got to send one.
const COULD_NOT_READ =
  'We could not finish reading your letter. You can have us try again, or type the details in yourself.';

const OUTCOME_SUFFIX: {[key: string]: string} = {
  found: 'found',
  nothing_found: 'not in this letter',
  failed: 'we could not read this',
  cached: 'already saved',
  timed_out: 'ran out of time',
  kept_existing: 'we kept what was already there',
};

const OUTCOME_COLOR: {[key: string]: string} = {
  found: '#28a745',
  nothing_found: '#555555',
  failed: '#dc3545',
  cached: '#555555',
  timed_out: '#b8860b',
  kept_existing: '#555555',
};

// Run-level outcomes that are not bad news. Everything else paints red.
const CALM_RUN_OUTCOMES = [
  'run_finished',
  'run_already_have_details',
  'run_kept_your_details',
];

let startTime: number | null = null;
let timerInterval: ReturnType<typeof setInterval> | null = null;
let inactivityTimer: ReturnType<typeof setTimeout> | null = null;
let hardCapTimer: ReturnType<typeof setTimeout> | null = null;
let renderedTasks: Set<string> = new Set();
// True once a terminal state has been painted. Nothing may paint over it: a
// close arriving after the run-level frame is just the socket hanging up.
let settled = false;
let activeSocket: WebSocket | null = null;
let currentUrl = '';
let currentData: Record<string, unknown> = {};

function createStatusIndicator(): HTMLElement {
  const statusDiv = document.createElement('div');
  statusDiv.id = 'entity-status-indicator';
  // The panel is the live region: a screen reader hears each step as it is
  // added and hears the final state when it lands.
  statusDiv.setAttribute('role', 'status');
  statusDiv.setAttribute('aria-live', 'polite');
  statusDiv.style.cssText = `
    background: #f8f9fa;
    border: 2px solid #ADD100;
    border-radius: 8px;
    padding: 16px;
    margin: 16px 0;
    text-align: center;
  `;
  statusDiv.innerHTML = `
    <div id="entity-status-title" style="font-size: 1.1rem; font-weight: 600; color: #333; margin-bottom: 8px;">
      Reading your denial letter
    </div>
    <div id="entity-timer" aria-hidden="true" style="font-size: 0.9rem; color: #666; margin-bottom: 8px;">
      Time elapsed: 0s
    </div>
    <div id="entity-status-list" style="font-size: 0.85rem; color: #555; line-height: 1.6;">
      <div>Starting to read your letter...</div>
    </div>
    <div id="entity-status-actions" style="margin-top: 12px; display: none; gap: 0.75rem; justify-content: center; flex-wrap: wrap;"></div>
  `;
  return statusDiv;
}

function updateTimer(): void {
  if (!startTime) return;
  const elapsed = Math.floor((Date.now() - startTime) / 1000);
  const timerEl = document.getElementById('entity-timer');
  if (timerEl) {
    // aria-hidden on this element: a counter that ticks once a second inside a
    // polite live region would talk over everything else in it.
    timerEl.textContent = `Time elapsed: ${elapsed}s`;
  }
}

function stopTimers(): void {
  if (timerInterval) {
    clearInterval(timerInterval);
    timerInterval = null;
  }
  if (inactivityTimer) {
    clearTimeout(inactivityTimer);
    inactivityTimer = null;
  }
  if (hardCapTimer) {
    clearTimeout(hardCapTimer);
    hardCapTimer = null;
  }
}

function armInactivityTimer(): void {
  if (inactivityTimer) {
    clearTimeout(inactivityTimer);
  }
  inactivityTimer = setTimeout(() => {
    finish({outcome: 'run_failed', label: COULD_NOT_READ});
  }, INACTIVITY_MS);
}

// One step's line. Only a frame the server gave words for is rendered, so
// there is no branch here that could put a wire name in front of a person.
function renderStep(frame: ExtractionFrame): void {
  const label = frame.label;
  const outcome = frame.outcome || '';
  if (!label) {
    return;
  }
  const key = label + '|' + outcome;
  if (renderedTasks.has(key)) {
    return;
  }
  renderedTasks.add(key);
  const statusList = document.getElementById('entity-status-list');
  if (!statusList) {
    return;
  }
  const suffix = OUTCOME_SUFFIX[outcome];
  const item = document.createElement('div');
  item.style.cssText =
    'color: ' + (OUTCOME_COLOR[outcome] || '#555555') + '; margin: 4px 0;';
  item.textContent = suffix ? label + ': ' + suffix : label;
  statusList.appendChild(item);
}

function actionButton(text: string, submits: boolean): HTMLButtonElement {
  const button = document.createElement('button');
  // The continue control is a plain submit button inside the flow's own form,
  // so continuing is the person pressing a button and the page never navigates
  // on their behalf.
  button.type = submits ? 'submit' : 'button';
  button.textContent = text;
  button.className = submits ? 'btn btn-green' : 'btn btn-secondary';
  button.style.cssText = 'margin: 0 0.25rem;';
  return button;
}

// Paint the terminal state and offer the two ways out of it. Every outcome
// gets both: continuing was the only thing on offer before and retrying was
// the only thing missing.
function finish(frame: ExtractionFrame): void {
  if (settled) {
    return;
  }
  settled = true;
  stopTimers();
  if (activeSocket) {
    try {
      activeSocket.close();
    } catch (e) {
      console.debug('entity_fetcher: socket already closed');
    }
    activeSocket = null;
  }

  const outcome = frame.outcome || 'run_failed';
  const label = frame.label || COULD_NOT_READ;
  const calm = CALM_RUN_OUTCOMES.indexOf(outcome) >= 0;

  // The yellow block above this panel says "Analyzing your denial..." behind a
  // spinner with no end, and it is only true while a run is in flight. The
  // deleted auto-advance used to whisk it off screen about a second after a
  // successful run; with the auto-advance gone it would sit there under every
  // terminal state, telling the person we are still reading a letter the panel
  // below has just finished reporting on. Two answers on one page is worse
  // than the one false answer this work removes, so the run's own words are
  // the only ones left standing.
  const waitingMsg = document.getElementById('waiting-msg');
  if (waitingMsg) {
    waitingMsg.style.display = 'none';
  }

  const statusIndicator = document.getElementById('entity-status-indicator');
  if (statusIndicator) {
    statusIndicator.style.borderColor = calm ? '#28a745' : '#dc3545';
  }
  const titleEl = document.getElementById('entity-status-title');
  if (titleEl) {
    titleEl.textContent = label;
  }
  const timerEl = document.getElementById('entity-timer');
  if (timerEl) {
    timerEl.textContent = '';
  }

  const actions = document.getElementById('entity-status-actions');
  if (actions) {
    actions.innerHTML = '';
    const retry = actionButton('Try reading the letter again', false);
    retry.addEventListener('click', () => {
      startRun(true);
    });
    actions.appendChild(retry);
    // Both ways out on every state, and the same two controls: only the words
    // on the green one change. "Continue and type it in myself" under "we read
    // your letter and filled in what we found" told the person their good run
    // had left them with the typing to do.
    actions.appendChild(
      actionButton(
        calm ? 'Continue to the next page' : 'Continue and type it in myself',
        true,
      ),
    );
    actions.style.display = 'flex';
  }
}

function handleFrame(raw: string): void {
  if (settled) {
    // A terminal state is already on the page. A frame arriving after it (a
    // socket opened by a reconnect that was scheduled in the second before a
    // timer fired, say) must not append a step line under the final words:
    // renderStep has no opinion about what is already painted, so the guard
    // belongs here, in front of it.
    return;
  }
  // Frames can carry extracted denial entities (PHI) -- log only the size.
  console.debug('entity_fetcher: frame length', (raw || '').length);
  const trimmed = (raw || '').trim();
  if (!trimmed) {
    return;
  }
  let frame: ExtractionFrame;
  try {
    frame = JSON.parse(trimmed);
  } catch (e) {
    console.warn('entity_fetcher: unparseable frame');
    return;
  }
  if (!frame || typeof frame !== 'object' || !frame.outcome) {
    return;
  }
  if (frame.type === 'run' || frame.type === 'error') {
    finish(frame);
    return;
  }
  renderStep(frame);
}

function connect(retries: number): void {
  const ws = new WebSocket(currentUrl);
  activeSocket = ws;
  let receivedAnything = false;
  let resolved = false;

  const settleConnection = () => {
    if (resolved || settled) {
      return;
    }
    resolved = true;
    if (!receivedAnything && retries < MAX_CONNECT_RETRIES) {
      // Nothing arrived at all: a blip worth one more try. The inactivity and
      // hard-cap timers keep running across reconnects, so this can never
      // loop past the point where the page owes the person an answer.
      setTimeout(() => connect(retries + 1), 1000);
      return;
    }
    // The socket ended without a run-level frame. That is not success, and it
    // used to be read as one.
    finish({outcome: 'run_failed', label: COULD_NOT_READ});
  };

  ws.onopen = () => {
    ws.send(JSON.stringify(currentData));
  };
  ws.onmessage = (event) => {
    receivedAnything = true;
    armInactivityTimer();
    handleFrame(event.data);
  };
  ws.onclose = () => settleConnection();
  ws.onerror = () => settleConnection();
}

function startRun(retry: boolean): void {
  settled = false;
  renderedTasks = new Set();
  stopTimers();

  const statusList = document.getElementById('entity-status-list');
  if (statusList) {
    statusList.innerHTML = '';
    const item = document.createElement('div');
    item.textContent = retry
      ? 'Reading your letter again...'
      : 'Starting to read your letter...';
    statusList.appendChild(item);
  }
  const titleEl = document.getElementById('entity-status-title');
  if (titleEl) {
    titleEl.textContent = 'Reading your denial letter';
  }
  const statusIndicator = document.getElementById('entity-status-indicator');
  if (statusIndicator) {
    statusIndicator.style.borderColor = '#ADD100';
  }
  const actions = document.getElementById('entity-status-actions');
  if (actions) {
    actions.innerHTML = '';
    actions.style.display = 'none';
  }

  currentData = {...currentData, retry: retry};

  startTime = Date.now();
  timerInterval = setInterval(updateTimer, 1000);
  armInactivityTimer();
  hardCapTimer = setTimeout(() => {
    finish({outcome: 'run_failed', label: COULD_NOT_READ});
  }, HARD_CAP_MS);

  connect(0);
}

export function doQuery(
  backend_url: string,
  data: Record<string, unknown>,
  retries: number,
) {
  currentUrl = backend_url;
  currentData = {...data};

  const waitingMsg = document.getElementById('waiting-msg');
  if (waitingMsg) {
    const statusIndicator = createStatusIndicator();
    waitingMsg.parentNode?.insertBefore(statusIndicator, waitingMsg.nextSibling);
  } else {
    const warningMsg = 'entity_fetcher.ts: waiting-msg not found; no status panel rendered.';
    console.warn(warningMsg);
    Sentry.captureMessage(warningMsg, 'warning');
  }

  startRun(false);
}

// Expose for invocation
(window as any).doQuery = doQuery;

// Wire up entity search enabling/disabling
document.addEventListener("DOMContentLoaded", () => {
  const inputElement = document.getElementById("entity_search") as HTMLInputElement | null;
  const resultsElement = document.getElementById("search_results") as HTMLElement | null;
  const searchSubmitButton = document.getElementById("submit_button") as HTMLButtonElement | null;

  if (!searchSubmitButton) {
    const warningMsg = 'entity_fetcher.ts:DOMContentLoaded - searchSubmitButton (id "submit_button") not found; cannot enable/disable next step control.';
    console.warn(warningMsg);
    Sentry.captureMessage(warningMsg, 'warning');
  } else {
    // disable initial state
    searchSubmitButton.disabled = true;
  }

  if (inputElement && resultsElement) {
    inputElement.addEventListener("input", () => {
      if (searchSubmitButton) {
        searchSubmitButton.disabled = inputElement.value.length === 0;
      } else {
        const warn = 'entity_fetcher.ts:DOMContentLoaded input handler - searchSubmitButton missing';
        console.warn(warn);
        Sentry.captureMessage(warn, 'warning');
      }
    });

    resultsElement.addEventListener("click", (event) => {
      const target = event.target as HTMLElement;
      if (target.tagName === "LI") {
        inputElement.value = target.innerText;
        if (searchSubmitButton) {
          searchSubmitButton.disabled = false;
        } else {
          const warn = 'entity_fetcher.ts:DOMContentLoaded results click handler - searchSubmitButton missing';
          console.warn(warn);
          Sentry.captureMessage(warn, 'warning');
        }
      }
    });
  }
});
