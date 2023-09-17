import { jsPDF } from 'jspdf';


async function generateAppealPDF() {

  const options = {
      orientation: 'p',
      unit: 'px',
      format: 'letter',
      };

  const text = document.getElementById("appeal_text").value;

  // Create a new jsPDF document
  const doc = new jsPDF(options);

  // Add the text box contents to the PDF document
  doc.text(20, 20, text, { maxWidth: 300 });

  doc.setProperties({
	title: 'Health Insurance Appeal'
   });

  // Save the PDF document and download it
  doc.save('appeal.pdf');
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
