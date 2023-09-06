import { storeLocal, pdfjsLib } from "./shared.ts"

async function generateAppealPDF() {

    const text = document.getElementById("appeal_text").value;

    const fileName = "appeal.pdf"; // the desired name of the PDF file

    // create a new PDF document
    const doc = new pdfjsLib.Document();

    // add a new page to the document
    const page = doc.addPage();

    // add the text to the page
    const textWidth = page.drawText(text, {
	size: 12,
    });

    // set the size of the document based on the text width and height
    const width = textWidth + 100;
    const height = page.getY() + 50;

    // convert the document to a PDF blob
    const blob = await doc.saveAsBlob();

    // create a download link for the PDF file
    const downloadLink = document.createElement("a");
    downloadLink.href = URL.createObjectURL(blob);
    downloadLink.download = fileName;

    // click the download link to download the file
    downloadLink.click();
}

function setupAppeal() {
    const generate_button = document.getElementById('generate_pdf');
    generate_button.onclick = async () => {
	await generateAppealPDF();
    }

    const target = document.getElementById("subbed_appeal_text");
    const appeal_text = document.getElementById("appeal_text");
    appeal_text.oninput = async () => {
	target.value = appeal_text.value;
    }
}


setupAppeal();
