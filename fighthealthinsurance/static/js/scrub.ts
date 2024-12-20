import { storeLocal, pdfjsLib, node_module_path } from "./shared"

import { TextContent, TextItem } from 'pdfjs-dist/types/src/display/api';


// Tesseract
import Tesseract from 'tesseract.js';



async function getTesseractWorkerRaw(): Promise<Tesseract.Worker> {
    console.log("Loading tesseract worker.")
    const worker = await Tesseract.createWorker(
	'eng',
	1,
	{
	    corePath: node_module_path + "/tesseract.js-core/tesseract-core.wasm.js",
	    workerPath: node_module_path + "/tesseract.js/dist/worker.min.js",
	    logger: function(m){console.log(m);}
	});
    await worker.setParameters({
	tessedit_pageseg_mode: Tesseract.PSM.AUTO_OSD,
    });
    return worker;
}

const memoizeOne = require('async-memoize-one');
const getTesseractWorker = memoizeOne(getTesseractWorkerRaw);

// Main
function rehideHiddenMessage(name: string): void {
    document.getElementById(name).classList.remove('visible');
}
function showHiddenMessage(name: string): void {
    document.getElementById(name).classList.add('visible');
}
function hideErrorMessages(event: Event): void {
    const form = document.getElementById("fuck_health_insurance_form") as HTMLFormElement;
    if (form == null) {
	return
    }
    if(form.privacy.checked && form.personalonly.checked && form.tos.checked) {
	rehideHiddenMessage('agree_chk_error');
    }
    if (form.pii.checked) {
	rehideHiddenMessage('pii_error');
    }
    if (form.email.value.length > 1) {
	document.getElementById('email-label').style.color="";
	rehideHiddenMessage('email_error');
    }
    if (form.denial_text.value.length > 1) {
	document.getElementById('denial_text_label').style.color="";
	rehideHiddenMessage('need_denial');
    }
}
function validateScrubForm(event: Event): void {
    const form = event.target as HTMLFormElement;
    if(!form.privacy.checked || !form.personalonly.checked || !form.tos.checked) {
	showHiddenMessage('agree_chk_error');
    } else {
	rehideHiddenMessage('agree_chk_error');
    }
    if (!form.pii.checked) {
	showHiddenMessage('pii_error');
    } else {
	rehideHiddenMessage('pii_error');
    }
    if (form.email.value.length < 1) {
	showHiddenMessage('email_error');
	document.getElementById('email-label').style.color="red";
    } else {
	document.getElementById('email-label').style.color="";
	rehideHiddenMessage('email_error');
    }
    if (form.denial_text.value.length < 1) {
	showHiddenMessage('need_denial');
	document.getElementById('denial_text_label').style.color="red";
    } else {
	document.getElementById('denial_text_label').style.color="";
	rehideHiddenMessage('need_denial');
    }

    if (form.pii.checked && form.privacy.checked && form.email.value.length > 0) {
	rehideHiddenMessage('agree_chk_error');
	rehideHiddenMessage('pii_error');
	rehideHiddenMessage('email_error');
	rehideHiddenMessage('need_denial');
	// YOLO
	return;
    } else {
	// Bad news no submit
	event.preventDefault();
    }
}

function addText(text: string): void {
    const input = document.getElementById("denial_text") as HTMLTextAreaElement;
    input.value += text;
}


function isPDF(file: File): boolean {
    return file.type.match('application/pdf') !== null;
}

async function getFileAsArrayBuffer(file: File): Promise<Uint8Array> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();

    reader.onload = () => {
	  if (reader.result instanceof ArrayBuffer) {
	      resolve(new Uint8Array(reader.result));
	  } else {
	      reject(new Error("Unexpected result type from FileReader"));
	  }
    };

    reader.onerror = () => {
      reject(reader.error);
    };

    reader.readAsArrayBuffer(file);
  });
}

const recognizePDF = async function(file: File) {
    const reader = new FileReader();
    const typedarray = await getFileAsArrayBuffer(file); 
    console.log("Data?")
    console.log(typedarray)
    const loadingTask = pdfjsLib.getDocument(typedarray);
    const doc = await loadingTask.promise;
    const pdfText = await getPDFText(doc);
    addText(pdfText);
    // Did we have almost no text? Try OCR
    if (pdfText.trim().length < 10) {
	const numPages = doc.numPages;
	const worker = await getTesseractWorker()

        for (let pageNo = 1; pageNo <= numPages; pageNo++) {
            const page = await doc.getPage(pageNo);
            const viewport = page.getViewport({ scale: 1.0 });
	    
            const canvas = document.createElement("canvas");
            const context = canvas.getContext("2d")!;
            canvas.height = viewport.height;
            canvas.width = viewport.width;
	    
            await page.render({ canvasContext: context, viewport }).promise;
	    
            const imageData = canvas.toDataURL("image/png");
            const ocrResult = await worker.recognize(imageData);
	    addText(ocrResult.data.text + "\n");
        }
    }
}

const recognizeImage = async function(file: File) {
    const worker = await getTesseractWorker()
    const ret = await worker.recognize(file);
    console.log("Recognize done!")
    console.log(ret.data.text);
    addText(ret.data.text);
}

const recognize = async function(evt: Event) {
    const input = evt.target as HTMLInputElement;
    const files = input.files;
    const filesArray = Array.from(files)

    for (const file of filesArray) {
	if (isPDF(file)) {
	    console.log("probably pdf")
	    try {
		await recognizePDF(file)
	    } catch {
		await recognizeImage(file)
	    }
	} else {
	    console.log("Assuming image...")
	    try {
		await recognizeImage(file)
	    } catch {
		await recognizePDF(file)
	    }
	}
    }
}


type ScrubRegex = [RegExp, string, string];
var scrubRegex: ScrubRegex[] = [
    [new RegExp("patents?:?\\s+(?<token>\\w+)", "gmi"), "name", "Patient: patient_name"],
    [new RegExp("patients?:?\\s+(?<token>\\w+)", "gmi"), "name", "Patient: patient_name"],
    [new RegExp("member:\\s+(?<token>\\w+)", "gmi"), "name", "Member: member_name"],
    [new RegExp("member:\\s+(?<token>\\w+\\s+\\w+)", "gmi"), "name", "Member: member_name"],
    [new RegExp("dear\\s+(?<token>\\w+\\s+\\w+)", "gmi"), "name", "Dear patient_name"],
    [new RegExp("dear\\s+(?<token>\\w+\\s+\\w+)\\s*\.?\\w+", "gmi"), "name", "Dear patient_name"],
    [new RegExp("dear\\s+(?<token>\\w+)", "gmi"), "name", "Dear patient_name"],
    [new RegExp("Subscriber\\s*ID\\s*.?\\s*.?\\s*(?<token>\\w+)", "gmi"), "subscriber_id", "Subscriber ID: subscribed_id"],
    [new RegExp("Group\\s*ID\\s*.?\\s*.?\\s*(?<token>\\w+)", "gmi"), "group_id", "Group ID: group_id"],
    [new RegExp("Group\\s*.?\\s*:\\s*(?<token>\\w+)", "gmi"), "group_id", "Group ID: group_id"],
    [new RegExp("Subscriber\\s*number\\s*.?\\s*.?\\s*(?<token>\\w+)", "gmi"), "subscriber_id", "Subscriber ID: subscribed_id"],
    [new RegExp("Group\\s*number\\s*.?\\s*.?\\s*(?<token>\\w+)", "gmi"), "group_id", "Group ID: group_id"],
];

function scrubText(text: string): string {
    var reservedTokens = [];
    var nodes = document.querySelectorAll('input');
    for (var i=0; i < nodes.length; i++) {
	var node = nodes[i];
	if (node.id.startsWith('store_') && node.value != "") {
	    reservedTokens.push(
		[new RegExp(node.value, "gi"),
		 node.id]);
	    for (var j=0; j<nodes.length; j++) {
		var secondNode = nodes[j];
		if (secondNode.value != "") {
		    reservedTokens.push(
			[new RegExp(node.value + secondNode.value, "gi"),
			 node.id + "_" + secondNode.id]);
		}
	    }
	}
    }
    console.log("Preparing to scrub:")
    console.log(text)
    console.log("Scrubbing with:")
    console.log(reservedTokens)
    console.log(scrubRegex)
    for (var i=0; i < scrubRegex.length; i++) {
	const match = scrubRegex[i][0].exec(text)
	if (match !== null) {
	    // I want to use the groups syntax here but it is not working so just index in I guess.
	    console.log("Match " + match + " groups " + match[1]);
	    console.log("Storing " + match[1] + " for " + scrubRegex[i][1]);
	    window.localStorage.setItem(scrubRegex[i][1], match[1]);
	}
	text = text.replace(scrubRegex[i][0], scrubRegex[i][2]);
    }
    for (var i=0; i < reservedTokens.length; i++) {
	text = text.replace(
	    reservedTokens[i][0],
	    " " + reservedTokens[i][1]);
    }
    return text;
}

function clean(): void {
    const denialText = document.getElementById("denial_text") as HTMLTextAreaElement; 
    denialText.value = scrubText(denialText.value);
}

function setupScrub(): void {
    // Restore previous local values
    var input = document.getElementsByTagName('input');
    console.log(input);
    var nodes: NodeListOf<HTMLInputElement> = document.querySelectorAll('input');
    console.log("Nodes:")
    console.log(nodes)
    function handleStorage(node: HTMLInputElement) {
	// All store_ fields which are local only and the e-mail field which is local and non-local.
	if (node.id.startsWith('store_') || node.id.startsWith("email")) {
	    node.addEventListener('change', storeLocal);
	    if (node.value == "") {
		node.value = window.localStorage.getItem(node.id);
	    }
	}
    }
    nodes.forEach(handleStorage);
    const elm = document.getElementById('uploader');
    if (elm != null) {
	elm.addEventListener('change', recognize);
    }
    const scrub = document.getElementById('scrub');
    if (scrub != null) {
	scrub.onclick = clean;
    }
    const scrub2 = document.getElementById('scrub-2');
    if (scrub2 != null) {
	scrub2.onclick = clean;
    }
    const form = document.getElementById("fuck_health_insurance_form") as HTMLFormElement;
    if (form) {
	form.addEventListener("submit", validateScrubForm);
	form.privacy.addEventListener("input", hideErrorMessages);
	form.personalonly.addEventListener("input", hideErrorMessages);
	form.tos.addEventListener("input", hideErrorMessages);
	form.pii.addEventListener("input", hideErrorMessages);
	form.email.addEventListener("input", hideErrorMessages);
	form.denial_text.addEventListener("input", hideErrorMessages);
    } else {
	console.log("Missing form?!?")
    }
}

async function getPDFPageText (pdf: any, pageNo: number): Promise<string> {
    const page = await pdf.getPage(pageNo);
    const tokenizedText: TextContent = await page.getTextContent();
    const items: TextItem[] = [];
    tokenizedText.items.forEach(item => {
	// Type guard to ensure item is of type TextItem
        if ('str' in item) {
            items.push(item as TextItem);
        }
    })
    const strs = items.map((token: TextItem) => token.str);
    const pageText = strs.join(" ");
    return pageText;
}

async function getPDFText(pdf: any): Promise<string> {
    console.log("Getting text from PDF:")
    console.log(pdf)
    const maxPages = pdf.numPages;
    console.log("Processing PDF pages:")
    console.log(maxPages)
    const pageTextPromises = [];
    for (let pageNo = 1; pageNo <= maxPages; pageNo += 1) {
	pageTextPromises.push(getPDFPageText(pdf, pageNo));
    }
    const pageTexts = await Promise.all(pageTextPromises);
    return pageTexts.join(" ");
};


setupScrub();
