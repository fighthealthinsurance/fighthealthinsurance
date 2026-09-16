declare const $: any;
import * as Sentry from '@sentry/browser';

// `label` carries the only words that may reach the page. A frame without one
// is never rendered, so no internal step name can be shown to a person.
interface ExtractionFrame {
  type?: string;
  task?: string;
  outcome?: string;
  label?: string;
  message?: string;
}

// A run that goes quiet is a run that failed.
const INACTIVITY_MS = 60000;
const HARD_CAP_MS = 120000;
const MAX_CONNECT_RETRIES = 2;

// The client's own words, for a run the server never got to describe.
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
// True once a terminal state has been painted. Nothing may paint over it.
let settled = false;
let activeSocket: WebSocket | null = null;
// Which run a socket belongs to. close() only asks the browser to hang up, and
// the close and message events of a superseded run keep landing for a network
// round trip after that, which is inside the window the retry button is on the
// screen for. A handler from an older generation does nothing.
let runGeneration = 0;
let currentUrl = '';
let currentData: Record<string, unknown> = {};

function createStatusIndicator(): HTMLElement {
  const statusDiv = document.createElement('div');
  statusDiv.id = 'entity-status-indicator';
  // The panel is the live region: a screen reader hears each step as it lands.
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
    // aria-hidden: a counter ticking once a second inside a polite live region
    // talks over everything else in it.
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
  // A submit inside the flow's own form: continuing is a press, never a
  // navigation the page performs.
  button.type = submits ? 'submit' : 'button';
  // Stable handles for the browser tests: the labels change with the outcome.
  button.id = submits ? 'entity-continue' : 'entity-retry';
  button.textContent = text;
  button.className = submits ? 'btn btn-green' : 'btn btn-secondary';
  button.style.cssText = 'margin: 0 0.25rem;';
  return button;
}

// Paint the terminal state. Every outcome offers both ways out of it.
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

  // The yellow block above says "Analyzing your denial..." behind an endless
  // spinner, so leaving it up would put a second, contradictory answer on the
  // page next to the run's own words.
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
    // renderStep has no opinion about what is already painted, so the guard
    // against appending a step line under the final words belongs here.
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
  const generation = runGeneration;
  const ws = new WebSocket(currentUrl);
  activeSocket = ws;
  let receivedAnything = false;
  let resolved = false;

  // True while this socket belongs to the run on the screen and that run still
  // wants work done. `settled` is half of it: a timer paints the terminal state
  // without touching the generation, so a reconnect booked just before it fired
  // would otherwise ask the server to read the letter again under that verdict.
  const current = () => generation === runGeneration && !settled;

  const settleConnection = () => {
    if (!current() || resolved) {
      return;
    }
    resolved = true;
    if (!receivedAnything && retries < MAX_CONNECT_RETRIES) {
      // The inactivity and hard-cap timers keep running across reconnects, so
      // this cannot loop past the point where the page owes an answer.
      setTimeout(() => {
        if (current()) {
          connect(retries + 1);
        }
      }, 1000);
      return;
    }
    // The socket ended without a run-level frame. That is not success.
    finish({outcome: 'run_failed', label: COULD_NOT_READ});
  };

  ws.onopen = () => {
    ws.send(JSON.stringify(currentData));
  };
  ws.onmessage = (event) => {
    if (!current()) {
      return;
    }
    receivedAnything = true;
    armInactivityTimer();
    handleFrame(event.data);
  };
  ws.onclose = () => settleConnection();
  ws.onerror = () => settleConnection();
}

function startRun(retry: boolean): void {
  settled = false;
  runGeneration += 1;
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
