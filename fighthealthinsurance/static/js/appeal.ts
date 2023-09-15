async function generateAppealPDF() {

    const text = document.getElementById("appeal_text").value;

    const fileName = "appeal.pdf"; // the desired name of the PDF file

    // create a new PDF document
    const doc = new pdfjsLib.PDFDocument();

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

function descrub() {
    const appeal_text = document.getElementById("scrubbed_appeal_text");
    const target = document.getElementById("appeal_text");
    var text = appeal_text.value;
    const fname = window.localStorage.getItem("store_fname");
    const lname = window.localStorage.getItem("store_lname");
    const name = fname + " " + lname;
    text = text.replace("fname", fname);
    text = text.replace("lname", fname);
    text = text.replace("[Your Name]", fname);
    target.value = text;
}

function setupAppeal() {
    const generate_button = document.getElementById('generate_pdf');
    generate_button.onclick = async () => {
	await generateAppealPDF();
    }

    appeal_text.oninput = descrub
    const descrub_button = document.getElementById('descrub');
    descrub_button.onclick = descrub
}


setupAppeal();
