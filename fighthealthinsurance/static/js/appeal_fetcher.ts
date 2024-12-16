// TypeScript Version

// Import necessary modules if using a framework like jQuery or update as needed.
declare const $: any;

// DOM Elements
const outputContainer = $('#output-container');
const loadingSpinner = document.getElementById('loading-spinner');
const loadingText = document.getElementById('loading-text');
const loadingMore = document.getElementById('loading-more');

// Variables
let respBuffer = '';
let appealId = 0;
let max_retries = 2;
const appealsSoFar: any[] = [];

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

function processResponseChunk(chunk: string): void {
    respBuffer += chunk;
    if (respBuffer.includes('\n')) {
        const lastIndex = respBuffer.lastIndexOf('\n');
        const current = respBuffer.substring(0, lastIndex);
        respBuffer = respBuffer.substring(lastIndex + 1);

        const lines = current.split('\n');
        lines.forEach((line) => {
            if (line.trim() === '') return;

            try {
                const parsedLine = JSON.parse(line);

                if (appealsSoFar.some((appeal) => JSON.stringify(appeal) === JSON.stringify(parsedLine))) {
                    console.log('Duplicate appeal found. Skipping.');
                    return;
                }

                appealsSoFar.push(parsedLine);
                appealId++;

                // Clone and configure the form
                const clonedForm = $('#base-form').clone().prop('id', `magic${appealId}`);
                clonedForm.removeAttr('style');

                const formElement = clonedForm.find('form');
                formElement.prop('id', `form_${appealId}`);

                const submitButton = clonedForm.find('button');
                submitButton.prop('id', `submit${appealId}`);

                const appealTextElem = clonedForm.find('textarea');
                appealTextElem.text(parsedLine);
                appealTextElem.val(parsedLine);
                appealTextElem.prop('form', `form_${appealId}`);
                appealTextElem[0].setAttribute('form', `form_${appealId}`);

                outputContainer.append(clonedForm);
            } catch (error) {
                console.error('Error parsing line:', error);
            }
        });
    } else {
        console.log('Waiting for more data.');
    }
}

export function doQuery(
  backend_url: string, 
  data: Map<string, string>, 
  retries: number
): void {
    if (retries > max_retries) {
	console.log("Error requesting backend");
	return;
    }
    showLoading();

    try {
	$.ajax({
            url: backend_url,
            type: 'POST',
            data: data,
            contentType: 'application/x-www-form-urlencoded',
            dataType: 'text',
            processData: true,
            xhr: function () {
		const xhr = new XMLHttpRequest();
		xhr.addEventListener('progress', (event) => {
                    const chunk = (event.currentTarget as XMLHttpRequest).responseText;
                    processResponseChunk(chunk);
		});
		return xhr;
            },
            success: (response: string) => {
		console.log('AJAX success:', response);
		processResponseChunk(response);

		// If we've reached stream end but also less than max_retries appeals retry
		if (appealsSoFar.length < 3 && retries < max_retries) {
		    console.error('Did not have expected number of appeals, retrying.');
                    doQuery(backend_url, data, retries + 1);
		} else {
                    hideLoading();
		}
            },
            error: (error: any) => {
		console.error('AJAX error:', error);
		if (retries < max_retries) {
                    doQuery(backend_url, data, retries + 1);
		} else {
                    hideLoading();
		}
            }
	});
    } catch (error) {
        console.error('Client-side error:', error);

        if (retries < max_retries) {
            console.log(`Retrying after client-side error... (${retries + 1}/${max_retries})`);
            doQuery(backend_url, data, retries + 1);
        } else {
            hideLoading();
        }
    }
}

// Make it available
(window as any).doQuery = doQuery;
