declare const $: any;

// DOM Elements
const outputContainer = $("#output-container");
const loadingSpinner = document.getElementById("loading-spinner");
const loadingText = document.getElementById("loading-text");
const loadingMore = document.getElementById("loading-more");

// Variables
let respBuffer = "";
let appealId = 0;
const appealsSoFar: any[] = [];
let retries = 0;
let maxRetries = 4;
let connectionStatus: 'connecting' | 'connected' | 'error' | 'done' = 'connecting';
let currentMessageIndex = 0;
let messageRotationInterval: ReturnType<typeof setTimeout> | null = null;
let startTime: number | null = null;
let timerInterval: ReturnType<typeof setInterval> | null = null;

// Humorous progress messages
const progressMessages = [
  "Reading your denial letter (and getting appropriately angry)...",
  "Digging through medical journals (the boring stuff)...",
  "Crafting arguments...",
  "Aiming precision word-missiles at insurance company weak spots...",
  "Channeling our inner Sir Humphrey Appleby (without the billable hours)...",
  "Using all available AI brain cells...",
  "Polishing your appeal to perfection...",
  "Almost done—preparing to launch your appeal into orbit..."
];

// Phase tracking
interface SubStep {
  id: string;
  label: string;
  state: 'pending' | 'done' | 'skipped' | 'error';
}

interface PhaseStep {
  id: string;
  label: string;
  substeps?: SubStep[];
  state: 'pending' | 'active' | 'done' | 'skipped';
  // Hidden phases are skipped by renderChecklist. The REST fallback step
  // starts hidden and is only revealed if/when we actually hand off to
  // the backup transport, so the normal happy path doesn't show it.
  hidden?: boolean;
}

const phases: PhaseStep[] = [
  { id: 'fallback', label: 'Switching to backup connection', state: 'pending', hidden: true },
  { id: 'init', label: 'Loading denial information', state: 'pending' },
  {
    id: 'research', label: 'Researching medical evidence', state: 'pending',
    substeps: [
      { id: 'pubmed', label: 'Searching PubMed', state: 'pending' },
      { id: 'citations', label: 'Generating citations', state: 'pending' },
      { id: 'guidelines', label: 'Looking up guidelines', state: 'pending' },
    ]
  },
  { id: 'generating', label: 'Generating appeals with AI', state: 'pending' },
  { id: 'synthesizing', label: 'Synthesizing best appeal', state: 'pending' },
];

interface PhaseIcon {
  char: string;
  color: string;
  className?: string;
}

function getPhaseIcon(state: string): PhaseIcon {
  switch (state) {
    case 'done': return { char: '\u2713', color: '#28a745' };
    case 'active': return { char: '', color: '', className: 'appeal-phase-spinner' };
    case 'skipped': return { char: '\u2014', color: '#999' };
    case 'error': return { char: '\u2717', color: '#dc3545' };
    default: return { char: '\u25CB', color: '#ccc' };
  }
}

function renderChecklist(): void {
  const checklist = document.getElementById('phase-checklist');
  if (!checklist) return;

  checklist.textContent = '';
  for (const phase of phases) {
    if (phase.hidden) continue;
    const icon = getPhaseIcon(phase.state);
    const labelStyle = phase.state === 'active' ? 'font-weight: 600; color: #333;' :
                       phase.state === 'done' ? 'color: #28a745;' :
                       phase.state === 'skipped' ? 'color: #999; text-decoration: line-through;' : 'color: #999;';
    let labelText = phase.label;
    if (phase.id === 'generating') {
      labelText = `${phase.label} (${appealsSoFar.length}/3)`;
    }

    const row = document.createElement('div');
    row.setAttribute('style', `display: flex; align-items: center; gap: 8px; margin: 6px 0; ${labelStyle}`);
    const iconSpan = document.createElement('span');
    iconSpan.setAttribute('style', 'width: 20px; text-align: center; flex-shrink: 0;');
    iconSpan.textContent = icon.char;
    if (icon.color) iconSpan.style.color = icon.color;
    if (icon.className) iconSpan.classList.add(icon.className);
    const labelSpan = document.createElement('span');
    labelSpan.textContent = labelText;
    row.appendChild(iconSpan);
    row.appendChild(labelSpan);
    checklist.appendChild(row);

    if (phase.substeps) {
      for (const sub of phase.substeps) {
        const subIcon = getPhaseIcon(sub.state);
        const subStyle = sub.state === 'done' ? 'color: #28a745;' :
                         sub.state === 'skipped' ? 'color: #999;' : 'color: #888;';
        const subRow = document.createElement('div');
        subRow.setAttribute('style', `display: flex; align-items: center; gap: 8px; margin: 3px 0 3px 28px; font-size: 0.85rem; ${subStyle}`);
        const subIconSpan = document.createElement('span');
        subIconSpan.setAttribute('style', 'width: 16px; text-align: center; flex-shrink: 0;');
        subIconSpan.textContent = subIcon.char;
        if (subIcon.color) subIconSpan.style.color = subIcon.color;
        if (subIcon.className) subIconSpan.classList.add(subIcon.className);
        const subLabelSpan = document.createElement('span');
        subLabelSpan.textContent = sub.label;
        subRow.appendChild(subIconSpan);
        subRow.appendChild(subLabelSpan);
        checklist.appendChild(subRow);
      }
    }
  }
}

function markPhasesUpTo(phaseId: string): void {
  for (const phase of phases) {
    if (phase.id === phaseId) break;
    // A still-hidden fallback step isn't part of the visible progression,
    // so don't auto-complete it just because a later phase started.
    if (phase.hidden) continue;
    if (phase.state !== 'done' && phase.state !== 'skipped') {
      phase.state = 'done';
      if (phase.substeps) {
        for (const sub of phase.substeps) {
          if (sub.state === 'pending') sub.state = 'done';
        }
      }
    }
  }
}

function setPhaseActive(phaseId: string): void {
  const phase = phases.find(p => p.id === phaseId);
  if (phase && phase.state !== 'done' && phase.state !== 'skipped') {
    phase.state = 'active';
  }
}

function markSubstepState(phaseId: string, substepId: string, state: SubStep['state'] = 'done'): void {
  const phase = phases.find(p => p.id === phaseId);
  if (phase && phase.substeps) {
    if (substepId === 'all') {
      for (const sub of phase.substeps) {
        sub.state = state;
      }
    } else {
      const sub = phase.substeps.find(s => s.id === substepId);
      if (sub) sub.state = state;
    }
  }
}

function markAllDone(): void {
  for (const phase of phases) {
    phase.state = 'done';
    if (phase.substeps) {
      for (const sub of phase.substeps) {
        if (sub.state === 'pending') sub.state = 'done';
      }
    }
  }
}

// Reveal the "Switching to backup connection" step in the checklist and
// mark it active. Called when we hand the WebSocket off to the REST
// fallback so the user can see why generation is still going. Once the
// REST stream starts emitting phase frames, markPhasesUpTo() flips this
// to done as the real work re-progresses.
function activateFallbackPhase(): void {
  const fallback = phases.find((p) => p.id === 'fallback');
  if (fallback) {
    fallback.hidden = false;
    if (fallback.state !== 'done') fallback.state = 'active';
  }
  renderChecklist();
}

// Shared helpers
function clearAllTimers(): void {
  if (messageRotationInterval) {
    clearTimeout(messageRotationInterval);
    messageRotationInterval = null;
  }
  if (timerInterval) {
    clearInterval(timerInterval);
    timerInterval = null;
  }
}

function fadeStatusMessage(text: string, color?: string): void {
  const el = document.getElementById('status-message');
  if (!el) return;
  el.style.opacity = '0';
  setTimeout(() => {
    el.textContent = text;
    if (color) el.style.color = color;
    el.style.opacity = '1';
  }, 200);
}

function createStatusIndicator(): HTMLElement {
  const statusDiv = document.createElement('div');
  statusDiv.id = 'appeal-status-indicator';
  statusDiv.style.cssText = `
    position: fixed;
    top: 80px;
    right: 20px;
    background: white;
    border: 2px solid #ADD100;
    border-radius: 12px;
    padding: 16px 20px;
    box-shadow: 0 4px 12px rgba(0,0,0,0.15);
    z-index: 1000;
    max-width: 340px;
    min-width: 280px;
    transition: all 0.3s ease;
  `;

  // Add spinner animation style
  const style = document.createElement('style');
  style.textContent = `
    @keyframes appeal-phase-spin {
      0% { transform: rotate(0deg); }
      100% { transform: rotate(360deg); }
    }
    .appeal-phase-spinner {
      display: inline-block;
      width: 14px;
      height: 14px;
      border: 2px solid #ADD100;
      border-top-color: transparent;
      border-radius: 50%;
      animation: appeal-phase-spin 0.8s linear infinite;
    }
  `;
  document.head.appendChild(style);

  statusDiv.innerHTML = `
    <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px;">
      <div style="font-weight: 600; font-size: 1rem; color: #333;">Generating Your Appeals</div>
      <div id="appeal-timer" style="font-size: 0.8rem; color: #888;"></div>
    </div>
    <div id="phase-checklist" style="margin-bottom: 12px;"></div>
    <div style="margin-bottom: 8px; display: flex; align-items: center; gap: 8px;">
      <div style="flex: 1; background: #e0e0e0; border-radius: 10px; height: 8px; overflow: hidden;">
        <div id="status-progress" style="background: linear-gradient(to right, #ADD100, #7B920A); height: 100%; width: 0%; transition: width 0.5s ease;"></div>
      </div>
      <div id="status-count" style="font-size: 0.85rem; font-weight: 600; color: #7B920A;">0/3</div>
    </div>
    <div id="status-message" style="font-size: 0.8rem; color: #888; line-height: 1.4; font-style: italic; transition: opacity 0.2s ease;">${progressMessages[0]}</div>
  `;
  return statusDiv;
}

function updateTimer(): void {
  if (!startTime) return;
  const elapsed = Math.floor((Date.now() - startTime) / 1000);
  const timerEl = document.getElementById('appeal-timer');
  if (timerEl) {
    timerEl.textContent = `${elapsed}s`;
  }
}

function updateStatusIndicator(status: typeof connectionStatus, appealsCount: number): void {
  const statusProgress = document.getElementById('status-progress');
  const statusCount = document.getElementById('status-count');

  if (!statusProgress || !statusCount) return;

  connectionStatus = status;

  if (status === 'done') {
    markAllDone();
    renderChecklist();
    statusProgress.style.width = '100%';
    statusCount.textContent = `${appealsCount}/3`;
    const statusMessage = document.getElementById('status-message');
    if (statusMessage) {
      statusMessage.textContent = 'Your appeals are ready! Choose the one you like best.';
      statusMessage.style.fontStyle = 'normal';
      statusMessage.style.color = '#28a745';
    }
    clearAllTimers();
    setTimeout(() => {
      const indicator = document.getElementById('appeal-status-indicator');
      if (indicator) indicator.style.opacity = '0';
      setTimeout(() => indicator?.remove(), 300);
    }, 3000);
    return;
  }

  if (status === 'error') {
    const statusMessage = document.getElementById('status-message');
    if (statusMessage) {
      statusMessage.textContent = `Connection issue. Retrying (${retries}/${maxRetries})...`;
      statusMessage.style.color = '#dc3545';
    }
  }

  const progress = Math.min((appealsCount / 3) * 100, 100);
  statusProgress.style.width = `${progress}%`;
  statusCount.textContent = `${appealsCount}/3`;
}

function rotateStatusMessage(): void {
  const statusMessage = document.getElementById('status-message');
  if (statusMessage && connectionStatus === 'connected') {
    currentMessageIndex = (currentMessageIndex + 1) % progressMessages.length;
    fadeStatusMessage(progressMessages[currentMessageIndex]);
  }
}

function showLoading(): void {
  if (loadingSpinner && loadingText) {
    loadingSpinner.style.display = "block";
    loadingText.style.display = "block";
  }

  // Reset phase states for retry
  for (const phase of phases) {
    phase.state = 'pending';
    // Re-hide the fallback step so a fresh WebSocket attempt (e.g. the
    // external-models rerun) doesn't show a stale "backup connection" row.
    if (phase.id === 'fallback') phase.hidden = true;
    if (phase.substeps) {
      for (const sub of phase.substeps) sub.state = 'pending';
    }
  }

  // Add status indicator
  const existingIndicator = document.getElementById('appeal-status-indicator');
  if (!existingIndicator) {
    const statusIndicator = createStatusIndicator();
    document.body.appendChild(statusIndicator);
  }
  renderChecklist();
  updateStatusIndicator('connecting', 0);

  // Start timer
  startTime = Date.now();
  if (timerInterval) clearInterval(timerInterval);
  timerInterval = setInterval(updateTimer, 1000);

  // Rotate messages every 8 seconds with sub-1-second jitter
  if (messageRotationInterval) {
    clearTimeout(messageRotationInterval);
  }
  const scheduleNextRotation = () => {
    const jitter = Math.random() * 800;
    messageRotationInterval = setTimeout(() => {
      rotateStatusMessage();
      scheduleNextRotation();
    }, 8000 + jitter);
  };
  scheduleNextRotation();
}

function hideLoading(): void {
  clearAllTimers();

  if (loadingSpinner && loadingText && loadingMore) {
    setTimeout(() => {
      loadingSpinner.style.display = "none";
      loadingText.style.display = "none";
      loadingMore.style.display = "none";
    }, 1000);
  }
}

// Hacky, TODO: Fix this.
let my_data: Map<string, string> = new Map<string, string>();
let my_backend_url = "";
let my_rest_fallback_url = "";
let usingRestFallback = false;
// Set when ws.onerror has decided to delegate to REST, so the
// concurrent ws.onclose doesn't run done() in parallel and tear
// down the UI while the REST stream is still in flight.
let handingOffToRest = false;
let hasAutoScrolledToFirstAppeal = false;

// Diagnostic counters used in the client error report so server logs
// can distinguish "we never reached the server" from "we reached the
// server, got status frames, but never saw an appeal payload".
let statusMessagesReceived = 0;
let lastPhaseReceived = '';
let serverReportedTotalAppeals = -1;
let serverReportedNewAppeals = -1;
let serverReportedExistingAppeals = -1;
let lastWsCloseCode = -1;
let lastWsCloseReason = '';
let lastWsErrorMessage = '';
let lastRestErrorMessage = '';
// Count of appeals the client deduped against ones already shown. The
// server counts these in total_appeals, so we offset the partial-delivery
// check by this value — otherwise a legitimately-duplicated synthesis
// would page Sentry every time.
let duplicatesSkipped = 0;

// Wait-time tracking so Sentry shows how long the user waited
// before we gave up. `doQueryStartedAtMs` is the moment doQuery()
// was first called (page load -> appeals page); attemptDurationsMs
// collects each WS attempt + the REST fallback in order, so triage
// can see whether one attempt timed out vs many failed quickly.
let doQueryStartedAtMs = 0;
let currentAttemptStartedAtMs = 0;
const attemptDurationsMs: number[] = [];

function endCurrentAttempt(): void {
  if (currentAttemptStartedAtMs > 0) {
    attemptDurationsMs.push(Date.now() - currentAttemptStartedAtMs);
    currentAttemptStartedAtMs = 0;
  }
}

function beginAttempt(): void {
  currentAttemptStartedAtMs = Date.now();
}

// Inactivity timeout: if the WebSocket goes this long without ANY frame
// (not even a keep-alive newline) the stream is treated as stalled. The
// backend interleaves keep-alives during generation/synthesis and the
// longest expected quiet stretch (the research phase) is ~50s, so 90s is
// comfortably above normal cadence while still well under the hard cap.
const WS_INACTIVITY_TIMEOUT_MS = 90000;
// Hard cap on the WebSocket transport. Even if frames are trickling in,
// once we've spent this long without finishing we stop waiting on the
// socket and fire the backup REST request so the user isn't stuck.
const WS_HARD_TIMEOUT_MS = 120000;

let timeoutHandle: ReturnType<typeof setTimeout> | null = null;
// Absolute-deadline timer for the WebSocket transport (see escalateToRest).
let hardTimeoutHandle: ReturnType<typeof setTimeout> | null = null;
// The socket for the current attempt, so the module-level hard-timeout
// handler can close it when it decides to hand off to REST.
let activeWebSocket: WebSocket | null = null;

// Hand generation off from the WebSocket to the REST fallback. Returns
// true if it actually escalated. Idempotent and safe to call from any of
// the give-up paths (inactivity timeout, hard cap, exhausted retries):
// the handingOffToRest/usingRestFallback latches make repeat calls no-ops.
function escalateToRest(reason: string): boolean {
  if (!my_rest_fallback_url || usingRestFallback || handingOffToRest) {
    return false;
  }
  console.log(`Escalating to REST fallback: ${reason}`);
  handingOffToRest = true;
  // The WS transport is done; stop both of its timers so neither fires
  // again while the REST leg is streaming.
  if (timeoutHandle) {
    clearTimeout(timeoutHandle);
    timeoutHandle = null;
  }
  if (hardTimeoutHandle) {
    clearTimeout(hardTimeoutHandle);
    hardTimeoutHandle = null;
  }
  // Record the in-flight WS attempt's duration before fetchFallback's
  // beginAttempt() starts the REST leg's clock. Idempotent if a caller
  // already ended it.
  endCurrentAttempt();
  // Close the live socket so it can't keep streaming into the parser or
  // re-arm the inactivity timer after we've moved on. ws.onclose/onerror
  // short-circuit on handingOffToRest.
  if (activeWebSocket) {
    try {
      activeWebSocket.close();
    } catch (e) {
      /* ignore */
    }
    activeWebSocket = null;
  }
  // Drop any half-received line from the dead socket so it can't get
  // concatenated onto (and corrupt) the first frame of the REST stream.
  respBuffer = '';
  // Surface the transport switch in the status checklist.
  activateFallbackPhase();
  const csrfToken = (my_data as any).csrfmiddlewaretoken || '';
  fetchFallback(my_rest_fallback_url, my_data, csrfToken);
  return true;
}

// Arm the absolute WebSocket deadline. Called once for the whole user
// wait (from the first doQuery), so it spans retries; it is NOT re-armed
// per attempt. On fire: hand off to REST, or — if there's no REST URL —
// stop the WS retry loop so we don't spin past the budget.
function armHardTimeout(): void {
  if (hardTimeoutHandle) return;
  hardTimeoutHandle = setTimeout(() => {
    hardTimeoutHandle = null;
    if (connectionStatus === 'done') return;
    console.error(
      `WebSocket exceeded ${WS_HARD_TIMEOUT_MS / 1000}s; firing backup REST request.`,
    );
    if (escalateToRest('websocket hard timeout (taking too long)')) {
      return;
    }
    // No REST fallback available (or already on it). If we're not already
    // riding the REST leg, force the retry loop to terminate rather than
    // letting slow WS attempts keep the user waiting indefinitely.
    if (!usingRestFallback && !handingOffToRest) {
      retries = maxRetries;
      if (activeWebSocket) {
        try {
          activeWebSocket.close();
        } catch (e) {
          /* ignore */
        }
        activeWebSocket = null;
      }
      endCurrentAttempt();
      const statusMessage = document.getElementById('status-message');
      if (statusMessage) {
        statusMessage.textContent =
          'This is taking longer than expected. Please try refreshing the page.';
        statusMessage.style.color = '#dc3545';
      }
      done();
    }
  }, WS_HARD_TIMEOUT_MS);
}

function parseValidDenialId(value: unknown): number | null {
  const asString = String(value ?? '').trim();
  if (!/^\d+$/.test(asString)) {
    return null;
  }
  const parsed = Number(asString);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null;
}

function reportClientError(error: string): void {
  const denialIdRaw = (my_data as any).denial_id || (my_data as any).get?.('denial_id') || '';
  const denialId = parseValidDenialId(denialIdRaw);
  const csrfToken = (my_data as any).csrfmiddlewaretoken || '';
  // Wait times: aggregate is page-time-since-doQuery. Per-attempt is
  // each completed WS attempt + the REST fallback. If we're still
  // inside an attempt when reporting, surface that too so we can tell
  // "stuck forever on attempt 1" apart from "many quick failures".
  const aggregateWaitMs = doQueryStartedAtMs > 0
    ? Date.now() - doQueryStartedAtMs : -1;
  const inflightWaitMs = currentAttemptStartedAtMs > 0
    ? Date.now() - currentAttemptStartedAtMs : -1;
  // Delivery diagnostics live in their own field so a long mobile
  // user-agent string in browser_info doesn't truncate them.
  const diagnostics = [
    `appeals_received=${appealsSoFar.length}`,
    `dupes_skipped=${duplicatesSkipped}`,
    `retries=${retries}`,
    `status_msgs=${statusMessagesReceived}`,
    `last_phase=${lastPhaseReceived || 'none'}`,
    `server_total=${serverReportedTotalAppeals}`,
    `server_new=${serverReportedNewAppeals}`,
    `server_existing=${serverReportedExistingAppeals}`,
    `ws_close=${lastWsCloseCode}/${lastWsCloseReason || 'none'}`,
    `ws_err=${lastWsErrorMessage || 'none'}`,
    `rest_fallback=${usingRestFallback}`,
    `rest_err=${lastRestErrorMessage || 'none'}`,
    `wait_total_ms=${aggregateWaitMs}`,
    `wait_attempts_ms=${attemptDurationsMs.join(',') || 'none'}`,
    `wait_inflight_ms=${inflightWaitMs}`,
  ].join(' ');
  const browserInfo = `${navigator.userAgent} | ${window.location.pathname} | ref=${document.referrer || 'none'}`;
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (csrfToken) {
    headers['X-CSRFToken'] = csrfToken;
  }
  if (denialId === null) {
    console.warn('Invalid denial ID format on client before error report', {
      denialIdRaw,
      sessionStorageKeys: Object.keys(window.sessionStorage || {}).slice(0, 10),
    });
  }
  fetch('/ziggy/rest/report_client_error', {
    method: 'POST',
    headers,
    body: JSON.stringify({
      denial_id: denialId ?? 'invalid',
      error,
      browser_info: browserInfo,
      diagnostics,
      denial_id_raw: String(denialIdRaw),
    }),
  }).catch(() => {}); // fire-and-forget
}

function done(): void {
  // If we've reached stream end but also less than maxRetries appeals retry.
  // Once we've fallen back to REST we stop retrying: REST is the last-resort
  // transport, so bouncing back to the flaky WebSocket would just spin.
  if (appealsSoFar.length < 3 && retries < maxRetries && !usingRestFallback) {
    console.error("Did not have expected number of appeals, retrying.");
    retries = retries + 1;
    doQuery(my_backend_url, my_data, my_rest_fallback_url);
  } else {
    if (appealsSoFar.length === 0) {
      const transport = usingRestFallback ? 'rest-fallback' : 'websocket';
      reportClientError(
        `Client exhausted ${retries} retries with 0 appeals received via ${transport}`
      );
    }
    clearTimeout(timeoutHandle!);
    if (hardTimeoutHandle) {
      clearTimeout(hardTimeoutHandle);
      hardTimeoutHandle = null;
    }
    hideLoading();
    // When internal-only generation underdelivered (0-2 appeals), the
    // template emits a hidden prompt offering to opt the denial into
    // external models. Reveal it and wire up the click.
    maybeShowExternalModelsPrompt();
  }
}

// Threshold for "we didn't generate enough appeals" - the regular flow
// targets 3 appeals, so 2 or fewer indicates underdelivery worth
// surfacing the external-models opt-in for.
const FEW_APPEALS_THRESHOLD = 2;

// Latched once the user has already opted in. Prevents re-showing the
// prompt if the post-opt-in rerun also underdelivers — the user has
// already made the call, repeating it doesn't help.
let externalModelsRequested = false;

function maybeShowExternalModelsPrompt(): void {
  if (externalModelsRequested) {
    return;
  }
  const prompt = document.getElementById("external-models-prompt");
  if (!prompt) {
    // Template didn't include the prompt - either external models are
    // already enabled, or we're on a page that doesn't have it.
    return;
  }
  if (appealsSoFar.length > FEW_APPEALS_THRESHOLD) {
    return;
  }
  if (prompt.style.display === "block") {
    return;
  }
  prompt.style.display = "block";
  const button = document.getElementById(
    "request-external-models-btn",
  ) as HTMLButtonElement | null;
  if (!button) return;
  // Deliberately NOT { once: true }: a transient opt-in failure re-enables
  // the button (see the catch in requestExternalModels) and the user must be
  // able to click again. The button.disabled flag set at the start of the
  // request guards against double-submit while one is in flight, and the
  // `display === "block"` guard above keeps this listener from being attached
  // more than once.
  button.addEventListener("click", () => {
    void requestExternalModels(button, prompt);
  });
}

async function requestExternalModels(
  button: HTMLButtonElement,
  prompt: HTMLElement,
): Promise<void> {
  const statusEl = document.getElementById("external-models-status");
  const setStatus = (text: string, color: string) => {
    if (statusEl) {
      statusEl.textContent = text;
      statusEl.style.color = color;
    }
  };
  const url = (window as any).enableExternalModelsUrl as string | undefined;
  if (!url) {
    setStatus("Unable to enable external models right now.", "#dc3545");
    return;
  }
  const csrfToken = (my_data as any).csrfmiddlewaretoken || "";
  button.disabled = true;
  setStatus("Enabling external models and re-running appeals...", "#333");
  try {
    const response = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrfToken,
      },
      body: JSON.stringify({
        denial_id: (my_data as any).denial_id || "",
        email: (my_data as any).email || "",
        semi_sekret: (my_data as any).semi_sekret || "",
      }),
    });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}: ${response.statusText}`);
    }
    externalModelsRequested = true;
    setStatus(
      "External models enabled. Generating additional appeals...",
      "#28a745",
    );
    prompt.style.display = "none";
    // Reset retry/parser state so doQuery starts a fresh generation
    // pass rather than treating this as a retry of the previous
    // attempt. Deliberately do NOT clear appealsSoFar or
    // #output-container: any already-shown internal-only drafts stay
    // on screen for the user to read while external models stream
    // additional drafts. processResponseChunk dedups against
    // appealsSoFar so server-side re-yielded ProposedAppeals don't
    // append twice, and the >=3 done() check counting old+new is
    // intentional — we want a total of 3 drafts, not 3 *new* ones.
    retries = 0;
    respBuffer = "";
    hasAutoScrolledToFirstAppeal = false;
    doQuery(my_backend_url, my_data, my_rest_fallback_url);
  } catch (error) {
    console.error("Failed to enable external models:", error);
    setStatus(
      "Could not enable external models. Please try again later.",
      "#dc3545",
    );
    button.disabled = false;
  }
}

// REST fallback for when WebSockets are unavailable (e.g. iPhone Safari, corporate proxies)
async function fetchFallback(url: string, data: object, csrfToken: string): Promise<void> {
  console.log("Using REST fallback for appeal generation");
  usingRestFallback = true;
  // ws.onerror already called endCurrentAttempt() before handoff, so
  // start a fresh attempt timer for the REST leg.
  beginAttempt();
  const statusMessage = document.getElementById('status-message');
  if (statusMessage) {
    statusMessage.textContent = 'Using alternative connection method...';
  }
  updateStatusIndicator('connected', appealsSoFar.length);

  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': csrfToken,
      },
      body: JSON.stringify(data),
    });

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}: ${response.statusText}`);
    }

    if (!response.body) {
      // No streaming body available — read entire response as text
      const text = await response.text();
      for (const line of text.split('\n')) {
        if (line.trim()) {
          processResponseChunk(line + '\n');
        }
      }
    } else {
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done: streamDone, value } = await reader.read();
        if (streamDone) break;
        buffer += decoder.decode(value, { stream: true });
        // Process complete lines
        while (buffer.includes('\n')) {
          const idx = buffer.indexOf('\n');
          const line = buffer.slice(0, idx + 1);
          buffer = buffer.slice(idx + 1);
          processResponseChunk(line);
        }
      }
      // Process any remaining data
      if (buffer.trim()) {
        processResponseChunk(buffer + '\n');
      }
    }
    updateStatusIndicator('done', appealsSoFar.length);
  } catch (error) {
    console.error("REST fallback error:", error);
    lastRestErrorMessage = (error as any)?.message || String(error);
    if (statusMessage) {
      statusMessage.textContent = 'Connection failed. Please try refreshing the page.';
      statusMessage.style.color = '#dc3545';
    }
  }
  // Record REST attempt duration before reporting / hiding UI.
  endCurrentAttempt();
  done();
}

function processResponseChunk(chunk: string): void {
  console.log(`Processing chunk ${chunk}`);
  respBuffer += chunk;
  if (respBuffer.includes("\n")) {
    const lastIndex = respBuffer.lastIndexOf("\n");
    const current = respBuffer.substring(0, lastIndex);
    respBuffer = respBuffer.substring(lastIndex + 1);

    const lines = current.split("\n");
    lines.forEach((line) => {
      // Skip the keep alive lines.
      if (line.trim() === "") return;

      try {
        const parsedLine = JSON.parse(line);

        // Handle status messages from backend
        if (parsedLine.type === 'status') {
          const phase = parsedLine.phase;
          const substep = parsedLine.substep;
          statusMessagesReceived++;
          if (phase) {
            lastPhaseReceived = phase;
          }

          if (phase === 'done') {
            // Explicit end-of-stream from server
            const total = parsedLine.total_appeals || 0;
            const newAppeals = parsedLine.new_appeals || 0;
            const existing = parsedLine.existing_appeals || 0;
            serverReportedTotalAppeals = total;
            serverReportedNewAppeals = newAppeals;
            serverReportedExistingAppeals = existing;
            // The server counts attempted yields; the client counts
            // distinct payloads. If we deduped some, account for them
            // here so a benign duplicate (e.g. synthesis returning a
            // verbatim draft) doesn't fire the partial-delivery alarm.
            const accountedFor = appealsSoFar.length + duplicatesSkipped;
            console.log(`Server stream complete: ${total} total appeals (${newAppeals} new, ${existing} existing), client has ${appealsSoFar.length} (+${duplicatesSkipped} deduped)`);
            if (total > 0 && appealsSoFar.length === 0 && duplicatesSkipped === 0) {
              console.error(`BUG: Server sent ${total} appeals but client received none!`);
              reportClientError(`Server sent ${total} appeals but client received 0 (lost in transit)`);
            } else if (total > accountedFor) {
              console.warn(`Partial delivery: server reported ${total} appeals, client got ${appealsSoFar.length} (+${duplicatesSkipped} deduped)`);
              reportClientError(`Partial delivery: server reported ${total} appeals, client got ${appealsSoFar.length}`);
            }
            markAllDone();
            renderChecklist();
            return;
          }

          if (phase) {
            // Mark all phases before this one as done
            markPhasesUpTo(phase);
            // Set this phase as active
            setPhaseActive(phase);

            // Handle substep completion or failure
            if (substep && (parsedLine.state === 'done' || parsedLine.state === 'error' || parsedLine.state === 'skipped')) {
              markSubstepState(phase, substep, parsedLine.state);
              // If all substeps were skipped, mark the parent phase as skipped too
              if (substep === 'all' && parsedLine.state === 'skipped') {
                const phaseObj = phases.find(p => p.id === phase);
                if (phaseObj) phaseObj.state = 'skipped';
              }
            }
          }

          // Update the humorous message area with the backend message
          fadeStatusMessage(parsedLine.message);

          renderChecklist();
          console.log('Backend status:', parsedLine.message);
          return;
        }

        // Server-side error frames from the REST fallback (and the
        // escalation WS) use {"type": "error", "message": "..."}.
        // Without this branch they'd fall through to the appeal-content
        // handler and push an `undefined` appeal into the UI.
        if (parsedLine.type === 'error') {
          console.error('Server reported error:', parsedLine.message);
          const msg = String(parsedLine.message || 'server error');
          // Attribute to the active transport so triage doesn't get
          // pointed at REST when the failure was on the WS path.
          if (usingRestFallback) {
            lastRestErrorMessage = msg;
          } else {
            lastWsErrorMessage = msg;
          }
          return;
        }

        // Handle regular appeal content
        const appealText = parsedLine.content;
        if (appealText === undefined || appealText === null) {
          console.warn('Skipping non-appeal frame without content', parsedLine);
          return;
        }

        if (
          appealsSoFar.some(
            (appeal) => JSON.stringify(appeal) === JSON.stringify(appealText),
          )
        ) {
          duplicatesSkipped++;
          console.log("Duplicate appeal found. Skipping.");
          return;
        }

        appealsSoFar.push(appealText);
        appealId++;

        const isSynthesized = parsedLine.synthesized === "true";

        // Update status indicator and checklist
        updateStatusIndicator('connected', appealsSoFar.length);
        renderChecklist();

        // Clone and configure the form
        const clonedForm = $("#base-form")
          .clone()
          .prop("id", `magic${appealId}`);
        clonedForm.removeAttr("style");

        // Add padding/margin to the cloned form container
        clonedForm.css({
          'padding': '0 20px',
          'margin': '20px 0'
        });

        // Add a badge for synthesized appeals
        if (isSynthesized) {
          const badge = document.createElement('div');
          badge.style.cssText = `
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 8px 16px;
            border-radius: 8px;
            font-weight: 600;
            font-size: 0.9rem;
            margin-bottom: 12px;
            display: inline-block;
          `;
          badge.textContent = '\u2728 AI-Optimized Appeal \u2014 Synthesized from all drafts';
          clonedForm.prepend(badge);
        }

        const formElement = clonedForm.find("form");
        formElement.prop("id", `form_${appealId}`);

        // Add max-width and auto margins to center the form with buffer space
        formElement.css({
          'max-width': '1200px',
          'margin': '0 auto',
          'padding': '0 20px'
        });

        const submitButton = clonedForm.find("button");
        submitButton.prop("id", `submit${appealId}`);

        const appealTextElem = clonedForm.find("textarea");
        appealTextElem.text(appealText);
        appealTextElem.val(appealText);
        appealTextElem.prop("form", `form_${appealId}`);
        appealTextElem[0].setAttribute("form", `form_${appealId}`);

        // Carry the saved ProposedAppeal row id (from save_appeal) back to
        // the backend so model attribution survives sub_in_appeals rewriting
        // the displayed text.
        const proposedId = parsedLine.id;
        if (proposedId !== undefined && proposedId !== null && proposedId !== "unknown") {
          clonedForm.find("input.proposed_appeal_id").val(String(proposedId));
        }

        outputContainer.append(clonedForm);

        if (!hasAutoScrolledToFirstAppeal) {
          hasAutoScrolledToFirstAppeal = true;
          (clonedForm[0] as HTMLElement).scrollIntoView({ behavior: "smooth", block: "start" });
        }
      } catch (error) {
        console.error("Error parsing line:", error);
      }
    });
  } else {
    console.log("Waiting for more data.");
    console.log(`So far:${respBuffer}`);
  }
}

// Different than entity fetcher version in that we use more globals so we can check state in done more easily.
function connectWebSocket(
  websocketUrl: string,
  data: object,
  processResponseChunk: (chunk: string) => void,
  done: () => void,
) {
  const startWebSocket = () => {
    // Start the per-attempt wait timer. connectWebSocket is called
    // recursively for retries, so each invocation gets its own start.
    beginAttempt();
    // Per-attempt flag: ws.onerror or the inactivity-timeout path may
    // decide to retry or hand off to REST. The same socket's
    // ws.onclose then fires on the normal error->close (or
    // close-after-explicit-close) sequence; without this guard,
    // onclose would also call done() and call endCurrentAttempt()
    // again, mangling the NEW attempt's duration and potentially
    // spawning overlapping retries via doQuery recursion.
    let retryScheduled = false;

    const ws = new WebSocket(websocketUrl);
    // Track the live socket so the module-level hard-timeout handler can
    // close it when it hands off to REST.
    activeWebSocket = ws;

    // Defined inside startWebSocket so it can close `ws` and mark
    // `retryScheduled` on the right per-attempt closure.
    const resetTimeout = () => {
      if (handingOffToRest || usingRestFallback) return;
      if (timeoutHandle) clearTimeout(timeoutHandle);
      timeoutHandle = setTimeout(() => {
        console.error(
          `No messages received in ${WS_INACTIVITY_TIMEOUT_MS / 1000} seconds. Stream stalled.`,
        );
        // Mark this socket stale, end its attempt timer, and explicitly
        // close it. ws.onclose will short-circuit on retryScheduled,
        // so it can't double-call done() or stomp on the next attempt.
        retryScheduled = true;
        endCurrentAttempt();
        try {
          ws.close();
        } catch (e) {
          /* ignore */
        }
        // A silent socket is usually dead, and reconnecting tends to stall
        // the same way — so go straight to the backup REST request rather
        // than burning the remaining time budget on more WS attempts.
        if (escalateToRest('websocket inactivity timeout')) {
          return;
        }
        // No REST fallback configured: fall back to the old retry/give-up
        // behaviour so we still terminate.
        if (retries < maxRetries) {
          retries++;
          setTimeout(
            () =>
              connectWebSocket(websocketUrl, data, processResponseChunk, done),
            1000,
          );
        } else {
          console.error("Max retries reached. Closing connection.");
          done();
        }
      }, WS_INACTIVITY_TIMEOUT_MS);
    };

    ws.onopen = () => {
      console.log("WebSocket connection opened");
      updateStatusIndicator('connected', appealsSoFar.length);
      ws.send(JSON.stringify(data));
      // Arm the inactivity timer now, not just on the first message: a
      // socket the server accepts but never replies on would otherwise
      // have no timeout, no error and no close — i.e. spin forever.
      resetTimeout();
    };

    // Handle incoming messages
    ws.onmessage = (event) => {
      // Ignore late frames once we've handed off to REST so we don't
      // re-arm the inactivity timer or append after the switch.
      if (handingOffToRest) return;
      resetTimeout();
      const chunk = event.data;
      processResponseChunk(chunk);
    };

    // Handle connection closure
    ws.onclose = (event) => {
      console.log("WebSocket connection closed:", event.code, event.reason);
      lastWsCloseCode = event.code;
      lastWsCloseReason = event.reason || '';
      // Two reasons to short-circuit done():
      //   1. handingOffToRest: REST fallback owns the final done() call
      //      and will record its own attempt duration.
      //   2. retryScheduled: ws.onerror or the inactivity-timeout path
      //      already queued a reconnect for this attempt and recorded
      //      its duration; calling done()/endCurrentAttempt() here
      //      would stomp on it.
      if (handingOffToRest || retryScheduled) {
        return;
      }
      // Normal close path: record the attempt duration before done().
      endCurrentAttempt();
      updateStatusIndicator('done', appealsSoFar.length);
      done();
    };

    // Handle errors
    ws.onerror = (error) => {
      // A reconnect/fallback was already scheduled for this socket (e.g. by
      // the inactivity timeout or hard cap closing it); don't double-handle.
      if (retryScheduled || handingOffToRest) return;
      console.error("WebSocket error:", error);
      lastWsErrorMessage = (error as any)?.message || (error as any)?.type || 'unknown';
      updateStatusIndicator('error', appealsSoFar.length);
      // Record this attempt's duration regardless of which branch we
      // take next (retry / fallback / give up).
      endCurrentAttempt();
      if (retries < maxRetries) {
        console.log(
          `Retrying WebSocket connection (${retries + 1}/${maxRetries})...`,
        );
        retries = retries + 1;
        retryScheduled = true;
        setTimeout(
          () =>
            connectWebSocket(websocketUrl, data, processResponseChunk, done),
          1000,
        );
      } else if (escalateToRest('websocket errored, retries exhausted')) {
        // REST fallback now owns the stream.
      } else {
        console.error("Max retries reached. Closing connection.");
        const statusMessage = document.getElementById('status-message');
        if (statusMessage) {
          statusMessage.textContent = 'Our AI took too many coffee breaks. Try refreshing the page.';
          statusMessage.style.color = '#dc3545';
        }
        done();
      }
    };
  };
  startWebSocket();
}

export function doQuery(backend_url: string, data: Map<string, string>, rest_fallback_url?: string): void {
  showLoading();
  my_backend_url = backend_url;
  my_data = data;
  my_rest_fallback_url = rest_fallback_url || '';
  usingRestFallback = false;
  handingOffToRest = false;
  // Start the aggregate wait clock only on the first call. doQuery
  // recurses on retry via done(), and we want the total to span the
  // entire user-visible wait, not just the latest retry. Arm the hard
  // WebSocket deadline alongside it so a slow-but-alive socket can't keep
  // the user waiting past the budget before we fire the backup REST.
  if (doQueryStartedAtMs === 0) {
    doQueryStartedAtMs = Date.now();
    armHardTimeout();
  }
  return connectWebSocket(backend_url, data, processResponseChunk, done);
}

// Make it available
(window as any).doQuery = doQuery;
