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
}

const phases: PhaseStep[] = [
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

let timeoutHandle: ReturnType<typeof setTimeout> | null = null;

function reportClientError(error: string): void {
  const denialId = (my_data as any).denial_id || (my_data as any).get?.('denial_id') || 'unknown';
  const csrfToken = (my_data as any).csrfmiddlewaretoken || '';
  const browserInfo = `${navigator.userAgent} | ${window.location.href}`;
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (csrfToken) {
    headers['X-CSRFToken'] = csrfToken;
  }
  fetch('/ziggy/rest/report_client_error', {
    method: 'POST',
    headers,
    body: JSON.stringify({
      denial_id: denialId,
      error,
      browser_info: browserInfo,
    }),
  }).catch(() => {}); // fire-and-forget
}

function done(): void {
  // If we've reached stream end but also less than maxRetries appeals retry
  if (appealsSoFar.length < 3 && retries < maxRetries) {
    console.error("Did not have expected number of appeals, retrying.");
    retries = retries + 1;
    doQuery(my_backend_url, my_data, my_rest_fallback_url);
  } else {
    if (appealsSoFar.length === 0) {
      reportClientError(`Client exhausted ${retries} retries with 0 appeals received`);
    }
    clearTimeout(timeoutHandle!);
    hideLoading();
  }
}

// REST fallback for when WebSockets are unavailable (e.g. iPhone Safari, corporate proxies)
async function fetchFallback(url: string, data: object, csrfToken: string): Promise<void> {
  console.log("Using REST fallback for appeal generation");
  usingRestFallback = true;
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
    if (statusMessage) {
      statusMessage.textContent = 'Connection failed. Please try refreshing the page.';
      statusMessage.style.color = '#dc3545';
    }
  }
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

          if (phase === 'done') {
            // Explicit end-of-stream from server
            const total = parsedLine.total_appeals || 0;
            const newAppeals = parsedLine.new_appeals || 0;
            const existing = parsedLine.existing_appeals || 0;
            console.log(`Server stream complete: ${total} total appeals (${newAppeals} new, ${existing} existing), client has ${appealsSoFar.length}`);
            if (total > 0 && appealsSoFar.length === 0) {
              console.error(`BUG: Server sent ${total} appeals but client received none!`);
              reportClientError(`Server sent ${total} appeals but client received 0`);
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

        // Handle regular appeal content
        const appealText = parsedLine.content;

        if (
          appealsSoFar.some(
            (appeal) => JSON.stringify(appeal) === JSON.stringify(appealText),
          )
        ) {
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

        outputContainer.append(clonedForm);
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
  const resetTimeout = () => {
    if (timeoutHandle) clearTimeout(timeoutHandle);
    timeoutHandle = setTimeout(() => {
      console.error("No messages received in 240 seconds. Reconnecting...");
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
    }, 240000); // 240 seconds timeout
  };

  const startWebSocket = () => {
    // Open the connection and send data
    const ws = new WebSocket(websocketUrl);
    ws.onopen = () => {
      console.log("WebSocket connection opened");
      updateStatusIndicator('connected', appealsSoFar.length);
      ws.send(JSON.stringify(data));
    };

    // Handle incoming messages
    ws.onmessage = (event) => {
      resetTimeout();
      const chunk = event.data;
      processResponseChunk(chunk);
    };

    // Handle connection closure
    ws.onclose = (event) => {
      console.log("WebSocket connection closed:", event.reason);
      updateStatusIndicator('done', appealsSoFar.length);
      done();
    };

    // Handle errors
    ws.onerror = (error) => {
      console.error("WebSocket error:", error);
      updateStatusIndicator('error', appealsSoFar.length);
      if (retries < maxRetries) {
        console.log(
          `Retrying WebSocket connection (${retries + 1}/${maxRetries})...`,
        );
        retries = retries + 1;
        setTimeout(
          () =>
            connectWebSocket(websocketUrl, data, processResponseChunk, done),
          1000,
        );
      } else if (my_rest_fallback_url && !usingRestFallback) {
        // WebSocket retries exhausted — try REST fallback
        console.log("WebSocket retries exhausted, falling back to REST endpoint");
        const csrfToken = (data as any).csrfmiddlewaretoken || '';
        fetchFallback(my_rest_fallback_url, data, csrfToken);
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
  return connectWebSocket(backend_url, data, processResponseChunk, done);
}

// Make it available
(window as any).doQuery = doQuery;
