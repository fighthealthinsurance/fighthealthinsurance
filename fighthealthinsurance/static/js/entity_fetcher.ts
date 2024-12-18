// TypeScript Version

// Import necessary modules if using a framework like jQuery or update as needed.
declare const $: any;

// DOM Elements
const loadingText = document.getElementById('waiting-msg');
const submitButton = document.getElementById("next");
const max_retries = 2;

function processResponseChunk(chunk: string): void {
    console.log("Waiting...")
}

function done(): void {
    submitButton.click();
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
                done();
            },
            error: (error: any) => {
		console.error('AJAX error:', error);
		if (retries < max_retries) {
                    doQuery(backend_url, data, retries + 1);
		} else {
                    done();
		}
            }
	});
    } catch (error) {
        console.error('Client-side error:', error);

        if (retries < max_retries) {
            console.log(`Retrying after client-side error... (${retries + 1}/${max_retries})`);
            doQuery(backend_url, data, retries + 1);
        } else {
            done();
        }
    }
}

// Make it available
(window as any).doQuery = doQuery;
