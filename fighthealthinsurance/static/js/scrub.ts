import { storeLocal, pdfjsLib } from "./shared.ts"

import Tesseract from 'tesseract.js';

// Main
function validateScrubForm(event) {
    const form = event.target;
    if(!form.privacy.checked) {
	document.getElementById('agree_chk_error').style.visibility='visible';
    } else {
	document.getElementById('agree_chk_error').style.visibility='hidden';
    }
    if (!form.pii.checked) {
	document.getElementById('pii_error').style.visibility='visible';
    } else {
	document.getElementById('pii_error').style.visibility='hidden';
    }
    if (form.pii.checked && form.privacy.checked) {
	document.getElementById('agree_chk_error').style.visibility='hidden';
	document.getElementById('pii_error').style.visibility='hidden';
	// YOLO
	return true;
    } else {
	// Bad news no submit
	event.preventDefault();
    }
}

function addText(text) {
    document.getElementById("denial_text").value += text;
}


function isPDF(file) {
    return file.type.match('application/pdf');
}

const recognize = async function(evt){
    const files = evt.target.files;
    const file = files[0];

    if (isPDF(file)) {
	console.log("PDF!")
	const reader = new FileReader();
	reader.onload = function () {
	    const typedarray = new Uint8Array(this.result);
	    console.log("Data?")
	    console.log(typedarray)
	    const loadingTask = pdfjsLib.getDocument(typedarray);
	    loadingTask.promise.then(doc => {
		const ret = getPDFText(doc);
		ret.then((t) => {
		    console.log("ret:");
		    console.log(ret);
		    addText(t);
		});
	    })
	};
	reader.readAsArrayBuffer(file);
    } else {
	console.log("Assuming image...")
	const worker = await getTesseractWorker()
	const ret = await worker.recognize(files[0]);
	console.log("Recognize done!")
	console.log(ret.data.text);
	addText(ret.data.text);
    }
}


var scrubRegex = [
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

function scrubText(text) {
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
	if (match != null) {
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
function clean() {
    document.getElementById("denial_text").value = scrubText(
	document.getElementById("denial_text").value);
}

function setupScrub()
{
    // Restore previous local values
    var input = document.getElementsByTagName('input');
    console.log(input);
    var nodes = document.querySelectorAll('input');
    console.log("Nodes:")
    console.log(nodes)
    function handleStorage(node) {
	if (node.id.startsWith('store_')) {
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
    scrub.onclick = clean;
    const form = document.getElementById("fuck_health_insurance_form");
    form.addEventListener("submit", validateScrubForm);
}

const memoizeOne = require('async-memoize-one');

// Tesseract
async function getTesseractWorkerRaw() {
    console.log("Loading tesseract worker.")
    const worker = await Tesseract.createWorker({
	corePath: node_module_path + "/tesseract.js-core/tesseract-core.wasm.js",
	workerPath: node_module_path + "/tesseract.js/dist/worker.min.js",
	logger: function(m){console.log(m);}
    });
    await worker.loadLanguage('eng');
    await worker.initialize('eng');
    await worker.setParameters({
	tessedit_pageseg_mode: Tesseract.PSM.PSM_AUTO_OSD,
    });
    return worker;
}

const getTesseractWorker = memoizeOne(getTesseractWorkerRaw);

async function getPDFPageText (pdf, pageNo) {
    const page = await pdf.getPage(pageNo);
    const tokenizedText = await page.getTextContent();
    const items = tokenizedText.items;
    const strs = items.map(token => token.str);
    const pageText = strs.join(" ");
    return pageText;
}

async function getPDFText(pdf) {
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
