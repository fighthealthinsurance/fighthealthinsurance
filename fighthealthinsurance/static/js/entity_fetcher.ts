declare const $: any;
import * as Sentry from '@sentry/browser';

// DOM Elements for WebSocket auto-advance
const nextButton = document.getElementById("next");

function processResponseChunk(chunk: string): void {
  console.log("Waiting...");
}

function done(): void {
  console.log("Moving to the next step :)");
  if (nextButton) {
    (nextButton as HTMLButtonElement).click();
  } else {
    const warningMsg = 'entity_fetcher.ts:done() - nextButton (id "next") not found; cannot auto-click to proceed.';
    console.warn(warningMsg);
    Sentry.captureMessage(warningMsg, 'warning');
  }
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
