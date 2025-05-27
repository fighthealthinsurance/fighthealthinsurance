declare const $: any;

// DOM Elements
const loadingText = document.getElementById("waiting-msg");
const submitButton = document.getElementById("next");

function processResponseChunk(chunk: string): void {
  console.log("Waiting...");
}

function done(): void {
  console.log("Moving to the next step :)");
  if (submitButton) {
    submitButton.click();
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

// Make it available
(window as any).doQuery = doQuery;

document.addEventListener("DOMContentLoaded", () => {
  const inputElement = document.getElementById(
    "entity_search",
  ) as HTMLInputElement | null;
  const resultsElement = document.getElementById(
    "search_results",
  ) as HTMLElement | null;
  const submitButton = document.getElementById(
    "submit_button",
  ) as HTMLButtonElement | null;

  if (submitButton) {
    submitButton.disabled = true;
  }

  if (inputElement && resultsElement) {
    inputElement.addEventListener("input", () => {
      if (submitButton) {
        if (inputElement.value.length > 0) {
          submitButton.disabled = false;
        } else {
          submitButton.disabled = true;
        }
      }
    });

    resultsElement.addEventListener("click", (event) => {
      const target = event.target as HTMLElement;
      if (target.tagName === "LI") {
        inputElement.value = target.innerText;
        if (submitButton) {
          submitButton.disabled = false;
        }
      }
    });
  }
});
