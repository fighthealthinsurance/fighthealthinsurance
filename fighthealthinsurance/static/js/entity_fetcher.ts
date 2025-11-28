declare const $: any;
import * as Sentry from '@sentry/browser';

// DOM Elements for WebSocket auto-advance
const nextButton = document.getElementById("next");
let startTime: number | null = null;
let timerInterval: ReturnType<typeof setInterval> | null = null;
let statusMessages: Set<string> = new Set();

function createStatusIndicator(): HTMLElement {
  const statusDiv = document.createElement('div');
  statusDiv.id = 'entity-status-indicator';
  statusDiv.style.cssText = `
    background: #f8f9fa;
    border: 2px solid #ADD100;
    border-radius: 8px;
    padding: 16px;
    margin: 16px 0;
    text-align: center;
  `;
  statusDiv.innerHTML = `
    <div style="font-size: 1.1rem; font-weight: 600; color: #333; margin-bottom: 8px;">
      🔍 Extracting Information from Your Denial
    </div>
    <div id="entity-timer" style="font-size: 0.9rem; color: #666; margin-bottom: 8px;">
      Time elapsed: 0s
    </div>
    <div id="entity-status-list" style="font-size: 0.85rem; color: #555; line-height: 1.6;">
      <div>⏳ Starting extraction...</div>
    </div>
  `;
  return statusDiv;
}

function updateTimer(): void {
  if (!startTime) return;
  const elapsed = Math.floor((Date.now() - startTime) / 1000);
  const timerEl = document.getElementById('entity-timer');
  if (timerEl) {
    timerEl.textContent = `Time elapsed: ${elapsed}s`;
  }
}

function updateStatusList(taskName: string): void {
  const taskDisplayNames: {[key: string]: string} = {
    'fax': 'Looking for fax information',
    'insurance company': 'Looking for insurance company',
    'plan id': 'Looking for plan ID',
    'claim id': 'Looking for the claim ID',
    'date of service': 'Looking for service date (if applicable)',
    'diagnosis': 'Looking up diagnosis',
    'type of denial': 'Identifying denial type (oh so many ways they try not to pay)'
  };

  const displayName = taskDisplayNames[taskName] || `✓ Processed ${taskName}`;

  if (!statusMessages.has(taskName)) {
    statusMessages.add(taskName);
    const statusList = document.getElementById('entity-status-list');
    if (statusList) {
      const item = document.createElement('div');
      item.style.cssText = 'color: #28a745; margin: 4px 0;';
      item.textContent = displayName;
      statusList.appendChild(item);

      // If diagnosis was extracted, add pubmed note
      if (taskName === 'diagnosis') {
        const pubmedNote = document.createElement('div');
        pubmedNote.style.cssText = 'color: #0066cc; margin: 8px 0; font-weight: 500;';
        pubmedNote.textContent = '📚 Looking up medical research in PubMed...';
        statusList.appendChild(pubmedNote);
      }
    }
  }
}

function processResponseChunk(chunk: string): void {
  console.log("Processing:", chunk);
  // Try to parse as task name
  if (chunk && chunk.length > 0 && chunk.length < 50) {
    updateStatusList(chunk.trim());
  }
}

function done(): void {
  console.log("Moving to the next step :)");

  // Stop the timer
  if (timerInterval) {
    clearInterval(timerInterval);
  }

  // Update status to complete
  const statusIndicator = document.getElementById('entity-status-indicator');
  if (statusIndicator) {
    statusIndicator.style.borderColor = '#28a745';
    const titleEl = statusIndicator.querySelector('div');
    if (titleEl) {
      titleEl.innerHTML = '✅ Extraction Complete!';
    }
  }

  // Auto-advance after a brief delay
  setTimeout(() => {
    if (nextButton) {
      (nextButton as HTMLButtonElement).click();
    } else {
      const warningMsg = 'entity_fetcher.ts:done() - nextButton (id "next") not found; cannot auto-click to proceed.';
      console.warn(warningMsg);
      Sentry.captureMessage(warningMsg, 'warning');
    }
  }, 1000);
}

function connectWebSocket(
  websocketUrl: string,
  data: object,
  processResponseChunk: (chunk: string) => void,
  done: () => void,
  retries = 0,
  maxRetries = 5,
) {
  const ws = new WebSocket(websocketUrl);

  // Open the connection and send data
  ws.onopen = () => {
    console.log("WebSocket connection opened");
    ws.send(JSON.stringify(data));
  };

  // Handle incoming messages
  ws.onmessage = (event) => {
    const chunk = event.data;
    processResponseChunk(chunk);
  };

  // Handle connection closure
  ws.onclose = (event) => {
    console.log("WebSocket connection closed:", event.reason);
    done();
  };

  // Handle errors
  ws.onerror = (error) => {
    console.error("WebSocket error:", error);
    if (retries < maxRetries) {
      console.log(
        `Retrying WebSocket connection (${retries + 1}/${maxRetries})...`,
      );
      setTimeout(
        () =>
          connectWebSocket(
            websocketUrl,
            data,
            processResponseChunk,
            done,
            retries + 1,
            maxRetries,
          ),
        1000,
      );
    } else {
      console.error("Max retries reached. Closing connection.");
      done();
    }
  };
}

export function doQuery(
  backend_url: string,
  data: Map<string, string>,
  retries: number,
) {
  // Initialize status indicator
  const waitingMsg = document.getElementById('waiting-msg');
  if (waitingMsg) {
    const statusIndicator = createStatusIndicator();
    waitingMsg.parentNode?.insertBefore(statusIndicator, waitingMsg.nextSibling);
  }

  // Start timer
  startTime = Date.now();
  timerInterval = setInterval(updateTimer, 1000);

  return connectWebSocket(
    backend_url,
    data,
    processResponseChunk,
    done,
    0,
    retries,
  );
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
