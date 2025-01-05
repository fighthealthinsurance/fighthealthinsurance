declare const $: any;

// DOM Elements
const loadingText = document.getElementById('waiting-msg');
const submitButton = document.getElementById("next");

function processResponseChunk(chunk: string): void {
    console.log("Waiting...")
}

function done(): void {
    console.log("Moving to the next step :)");
    submitButton.click();
}

function connectWebSocket(
    websocketUrl: string,
    data: object,
    processResponseChunk: (chunk: string) => void,
    done: () => void,
    retries = 0,
    maxRetries = 5
) {
    const ws = new WebSocket(websocketUrl);

    // Open the connection and send data
    ws.onopen = () => {
        console.log('WebSocket connection opened');
        ws.send(JSON.stringify(data));
    };

    // Handle incoming messages
    ws.onmessage = (event) => {
        const chunk = event.data;
        processResponseChunk(chunk);
    };

    // Handle connection closure
    ws.onclose = (event) => {
        console.log('WebSocket connection closed:', event.reason);
	done();
    };

    // Handle errors
    ws.onerror = (error) => {
       console.error('WebSocket error:', error);
       if (retries < maxRetries) {
            console.log(`Retrying WebSocket connection (${retries + 1}/${maxRetries})...`);
            setTimeout(() => connectWebSocket(websocketUrl, data, processResponseChunk, done, retries + 1, maxRetries), 1000);
        } else {
            console.error('Max retries reached. Closing connection.');
            done();
        }
    };
}

export function doQuery(
  backend_url: string, 
  data: Map<string, string>, 
  retries: number
): void {
    return connectWebSocket(
	backend_url,
	data,
	processResponseChunk,
	done,
	0,
	retries);
}

// Make it available
(window as any).doQuery = doQuery;
