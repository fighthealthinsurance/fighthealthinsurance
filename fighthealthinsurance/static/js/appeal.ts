import { jsPDF } from 'jspdf';

import {getLocalStorageItemOrDefault, getLocalStorageItemOrDefaultEQ} from './shared.ts';


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
    const fname = getLocalStorageItemOrDefault("store_fname", "FirstName");
    const lname = getLocalStorageItemOrDefault("store_lname", "LastName");
    const subscriber_id = getLocalStorageItemOrDefaultEQ("subscriber_id");
    const group_id = getLocalStorageItemOrDefaultEQ("group_id");
    const name = fname + " " + lname;
    text = text.replace("fname", fname);
    text = text.replace("lname", fname);
    text = text.replace("YourNameMagic", fname);
    text = text.replace("[Your Name]", fname);
    text = text.replace("subscriber_id", subscriber_id);
    text = text.replace("SCSID: 123456789", subscriber_id);
    text = text.replace("GPID: 987654321", group_id);
    text = text.replace("group_id", group_id);
    text = text.replace("GPID", group_id);
    text = text.replace("subscriber\\_id", subscriber_id);
    text = text.replace("group\\_id", group_id);
    target.value = text;
}

function printAppeal() {
    console.log("Starting to print.")
    const childWindow = window.open('','_blank','');
    childWindow.document.open();
    childWindow.document.write('<html><head></head><body>');
    childWindow.document.write(document.getElementById('appeal_text').value.replace(/\n/gi,'<br>'));
    childWindow.document.write('</body></html>');
    // Wait 1 second for chrome.
    setTimeout(function(){
	console.log("Executed after 1 second");
    }, 1000);
    // Does not seem to work on chrome still? But user can print from the window themselves.
    childWindow.print();
    console.log("Done!")
//    childWindow.document.close();
//    childWindow.close();
}

function setupAppeal() {
    const generate_button = document.getElementById('generate_pdf');
    generate_button.onclick = async () => {
	await generateAppealPDF();
    }

    const print_button = document.getElementById('print_appeal');
    print_button.onclick = async () => {
	await printAppeal();
    }

    appeal_text.oninput = descrub
    const descrub_button = document.getElementById('descrub');
    descrub_button.onclick = descrub
}


setupAppeal();
