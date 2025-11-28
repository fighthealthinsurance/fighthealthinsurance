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

// Humorous progress messages
const progressMessages = [
  "🤖 Waking up our AI friends from their digital naps...",
  "📝 Reading your denial letter (and getting appropriately angry)...",
  "🔍 Digging through medical journals (the boring stuff)...",
  "🤓 crafting arguments...",
  "🎯 Aiming precision word-missiles at insurance company weak spots...",
  "⚖️ Channeling our inner Sir Humphrey Appleby (without the billable hours)...",
  "🧠 Using all available AI brain cells...",
  "✍️ Polishing your appeal to perfection...",
  "🚀 Almost done—preparing to launch your appeal into orbit..."
];

// Helper Functions
function createStatusIndicator(): HTMLElement {
  const statusDiv = document.createElement('div');
  statusDiv.id = 'appeal-status-indicator';
  statusDiv.style.cssText = `
    position: fixed;
    top: 80px;
    right: 20px;
    background: white;
    border-radius: 12px;
    padding: 16px 20px;
    box-shadow: 0 4px 12px rgba(0,0,0,0.15);
    z-index: 1000;
    max-width: 320px;
    transition: all 0.3s ease;
  `;
  statusDiv.innerHTML = `
    <div style="display: flex; align-items: center; gap: 12px; margin-bottom: 8px;">
      <div id="status-icon" style="font-size: 24px;">🔄</div>
      <div>
        <div id="status-title" style="font-weight: 600; color: #333;">Generating Appeals</div>
        <div id="status-subtitle" style="font-size: 0.85rem; color: #666;">Connected</div>
      </div>
    </div>
    <div id="status-message" style="font-size: 0.9rem; color: #555; line-height: 1.4;">${progressMessages[0]}</div>
    <div style="margin-top: 12px; display: flex; align-items: center; gap: 8px;">
      <div style="flex: 1; background: #e0e0e0; border-radius: 10px; height: 8px; overflow: hidden;">
        <div id="status-progress" style="background: linear-gradient(to right, #ADD100, #7B920A); height: 100%; width: 0%; transition: width 0.5s ease;"></div>
      </div>
      <div id="status-count" style="font-size: 0.85rem; font-weight: 600; color: #7B920A;">0/3</div>
    </div>
  `;
  return statusDiv;
}

function updateStatusIndicator(status: typeof connectionStatus, appealsCount: number): void {
  const statusIcon = document.getElementById('status-icon');
  const statusTitle = document.getElementById('status-title');
  const statusSubtitle = document.getElementById('status-subtitle');
  const statusMessage = document.getElementById('status-message');
  const statusProgress = document.getElementById('status-progress');
  const statusCount = document.getElementById('status-count');

  if (!statusIcon || !statusTitle || !statusSubtitle || !statusMessage || !statusProgress || !statusCount) return;

  connectionStatus = status;

  switch (status) {
    case 'connecting':
      statusIcon.textContent = '🔄';
      statusTitle.textContent = 'Connecting';
      statusSubtitle.textContent = 'Establishing connection...';
      break;
    case 'connected':
      statusIcon.textContent = '🧠';
      statusTitle.textContent = 'Generating Appeals';
      statusSubtitle.textContent = 'AI working hard';
      break;
    case 'error':
      statusIcon.textContent = '⚠️';
      statusTitle.textContent = 'Connection Issue';
      statusSubtitle.textContent = `Retrying (${retries}/${maxRetries})...`;
      break;
    case 'done':
      statusIcon.textContent = '✅';
      statusTitle.textContent = 'Complete!';
      statusSubtitle.textContent = `Generated ${appealsCount} appeals`;
      statusMessage.textContent = '🎉 Your appeals are ready! Choose the one you like best.';
      statusProgress.style.width = '100%';
      statusCount.textContent = `${appealsCount}/3`;
      setTimeout(() => {
        const indicator = document.getElementById('appeal-status-indicator');
        if (indicator) indicator.style.opacity = '0';
        setTimeout(() => indicator?.remove(), 300);
      }, 3000);
      return;
  }

  const progress = Math.min((appealsCount / 3) * 100, 100);
  statusProgress.style.width = `${progress}%`;
  statusCount.textContent = `${appealsCount}/3`;
}

function rotateStatusMessage(): void {
  const statusMessage = document.getElementById('status-message');
  if (statusMessage && connectionStatus === 'connected') {
    currentMessageIndex = (currentMessageIndex + 1) % progressMessages.length;
    statusMessage.style.opacity = '0';
    setTimeout(() => {
      statusMessage.textContent = progressMessages[currentMessageIndex];
      statusMessage.style.opacity = '1';
    }, 200);
  }
}

function showLoading(): void {
  if (loadingSpinner && loadingText) {
    loadingSpinner.style.display = "block";
    loadingText.style.display = "block";
  }

  // Add status indicator
  const existingIndicator = document.getElementById('appeal-status-indicator');
  if (!existingIndicator) {
    const statusIndicator = createStatusIndicator();
    document.body.appendChild(statusIndicator);
  }
  updateStatusIndicator('connecting', 0);

  // Rotate messages every 8 seconds
  setInterval(rotateStatusMessage, 8000);
}

function hideLoading(): void {
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

let timeoutHandle: ReturnType<typeof setTimeout> | null = null;

function done(): void {
  // If we've reached stream end but also less than maxRetries appeals retry
  if (appealsSoFar.length < 3 && retries < maxRetries) {
    console.error("Did not have expected number of appeals, retrying.");
    retries = retries + 1;
    doQuery(my_backend_url, my_data);
  } else {
    clearTimeout(timeoutHandle!);
    hideLoading();
  }
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
          const statusMessage = document.getElementById('status-message');
          if (statusMessage) {
            statusMessage.style.opacity = '0';
            setTimeout(() => {
              statusMessage.textContent = parsedLine.message;
              statusMessage.style.opacity = '1';
            }, 200);
          }
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

        // Update status indicator
        updateStatusIndicator('connected', appealsSoFar.length);

        // Clone and configure the form
        const clonedForm = $("#base-form")
          .clone()
          .prop("id", `magic${appealId}`);
        clonedForm.removeAttr("style");

        const formElement = clonedForm.find("form");
        formElement.prop("id", `form_${appealId}`);

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
      console.error("No messages received in 95 seconds. Reconnecting...");
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
      } else {
        console.error("Max retries reached. Closing connection.");
        const statusMessage = document.getElementById('status-message');
        if (statusMessage) {
          statusMessage.textContent = '😴 Our AI took too many coffee breaks. Try refreshing the page.';
        }
        done();
      }
    };
  };
  startWebSocket();
}

export function doQuery(backend_url: string, data: Map<string, string>): void {
  showLoading();
  my_backend_url = backend_url;
  my_data = data;
  return connectWebSocket(backend_url, data, processResponseChunk, done);
}

// Make it available
(window as any).doQuery = doQuery;
