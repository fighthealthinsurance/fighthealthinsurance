declare const $: any;

// DOM Elements
const outputContainer = $('#output-container');
const loadingSpinner = document.getElementById('loading-spinner');
const loadingText = document.getElementById('loading-text');
const loadingMore = document.getElementById('loading-more');

// Variables
let respBuffer = '';
let appealId = 0;
const appealsSoFar: any[] = [];
let retries = 0;
let maxRetries = 4;

// Helper Functions
function showLoading(): void {
    if (loadingSpinner && loadingText) {
	loadingSpinner.style.display = 'block';
	loadingText.style.display = 'block';
    }
}

function hideLoading(): void {
    if (loadingSpinner && loadingText && loadingMore) {
	setTimeout(() => {
	    loadingSpinner.style.display = 'none';
	    loadingText.style.display = 'none';
	    loadingMore.style.display = 'none';
	}, 1000);
    }
}

// Hacky, TODO: Fix this.
let my_data: Map<string, string> = new Map<string, string>;
let my_backend_url = ""

let timeoutHandle: ReturnType<typeof setTimeout> | null = null;

function done(): void {
    // If we've reached stream end but also less than maxRetries appeals retry
    if (appealsSoFar.length < 3 && retries < maxRetries) {
	console.error('Did not have expected number of appeals, retrying.');
	retries = retries + 1;
	doQuery(my_backend_url, my_data);
    } else {
	clearTimeout(timeoutHandle);
	hideLoading();
    }
}

function processResponseChunk(chunk: string): void {
    console.log(`Processing chunk ${chunk}`)
    respBuffer += chunk;
    if (respBuffer.includes('\n')) {
	const lastIndex = respBuffer.lastIndexOf('\n');
	const current = respBuffer.substring(0, lastIndex);
	respBuffer = respBuffer.substring(lastIndex + 1);

	const lines = current.split('\n');
	lines.forEach((line) => {
	    // Skip the keep alive lines.
	    if (line.trim() === '') return;

	    try {
		const parsedLine = JSON.parse(line);
		const appealText = parsedLine.content;

		if (appealsSoFar.some((appeal) => JSON.stringify(appeal) === JSON.stringify(appealText))) {
		    console.log('Duplicate appeal found. Skipping.');
		    return;
		}

		appealsSoFar.push(appealText);
		appealId++;

		// Clone and configure the form
		const clonedForm = $('#base-form').clone().prop('id', `magic${appealId}`);
		clonedForm.removeAttr('style');

		const formElement = clonedForm.find('form');
		formElement.prop('id', `form_${appealId}`);

		const submitButton = clonedForm.find('button');
		submitButton.prop('id', `submit${appealId}`);

		const appealTextElem = clonedForm.find('textarea');
		appealTextElem.text(appealText);
		appealTextElem.val(appealText);
		appealTextElem.prop('form', `form_${appealId}`);
		appealTextElem[0].setAttribute('form', `form_${appealId}`);

		outputContainer.append(clonedForm);
	    } catch (error) {
		console.error('Error parsing line:', error);
	    }
	});
    } else {
	console.log('Waiting for more data.');
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
	    console.error('No messages received in 95 seconds. Reconnecting...');
	    if (retries < maxRetries) {
		retries++;
		setTimeout(() => connectWebSocket(websocketUrl, data, processResponseChunk, done), 1000);
	    } else {
		console.error('Max retries reached. Closing connection.');
		done();
	    }
	}, 95000); // 95 seconds timeout
    };

    const startWebSocket = () => {
	// Open the connection and send data
	const ws = new WebSocket(websocketUrl);
	ws.onopen = () => {
	    console.log('WebSocket connection opened');
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
	    console.log('WebSocket connection closed:', event.reason);
	    done();
	};

	// Handle errors
	ws.onerror = (error) => {
	    console.error('WebSocket error:', error);
	    if (retries < maxRetries) {
		console.log(`Retrying WebSocket connection (${retries + 1}/${maxRetries})...`);
		retries = retries + 1;
		setTimeout(() => connectWebSocket(websocketUrl, data, processResponseChunk, done), 1000);
	    } else {
		console.error('Max retries reached. Closing connection.');
		done();
	    }
	};
    };
    startWebSocket();
}


export function doQuery(
  backend_url: string,
  data: Map<string, string>,
): void {
    showLoading();
    my_backend_url = backend_url;
    my_data = data;
    return connectWebSocket(
	backend_url,
	data,
	processResponseChunk,
	done)
}

// Make it available
(window as any).doQuery = doQuery;
