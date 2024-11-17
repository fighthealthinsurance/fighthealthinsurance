import { jsPDF, jsPDFOptions } from 'jspdf';

import {getLocalStorageItemOrDefault, getLocalStorageItemOrDefaultEQ} from './shared';


async function generateAppealPDF() {

  const options: jsPDFOptions = {
      orientation: 'p', // portrait
      unit: 'px',
      format: 'letter',
      };

  const completedAppealText = (document.getElementById("id_completed_appeal_text") as HTMLTextAreaElement)?.value || "";


  // Create a new jsPDF document
  const doc = new jsPDF(options);

  // Add the text box contents to the PDF document
  doc.text(completedAppealText, 20, 20, { maxWidth: 300 });

  doc.setProperties({
	title: 'Health Insurance Appeal'
   });

  // Save the PDF document and download it
  doc.save('appeal.pdf');
}

function descrub() {
    const appeal_text = document.getElementById("scrubbed_appeal_text");
    const target = document.getElementById("id_completed_appeal_text") as HTMLTextAreaElement;
    var text = (appeal_text as HTMLTextAreaElement)?.value || "";
    const fname = getLocalStorageItemOrDefault("store_fname", "FirstName");
    const lname = getLocalStorageItemOrDefault("store_lname", "LastName");
    const subscriber_id = getLocalStorageItemOrDefaultEQ("subscriber_id");
    const group_id = getLocalStorageItemOrDefaultEQ("group_id");
    const email_address = getLocalStorageItemOrDefaultEQ("email_address");
    const name = fname + " " + lname;
    text = text.replace("fname", fname);
    text = text.replace("lname", fname);
    text = text.replace("YourNameMagic", fname);
    text = text.replace("[Your Name]", name);
    text = text.replace("[Patient's Name]", name);
    text = text.replace("$your_name_here", name);
    text = text.replace("subscriber_id", subscriber_id);
    text = text.replace("[Policy Number or Member ID]", subscriber_id);
    text = text.replace("[Email Address]", email_address);
    text = text.replace("SCSID: 123456789", subscriber_id);
    text = text.replace("GPID: 987654321", group_id);
    text = text.replace("group_id", group_id);
    text = text.replace("GPID", group_id);
    text = text.replace("subscriber\\_id", subscriber_id);
    text = text.replace("group\\_id", group_id);
    if (target) {
	target.value = text;
    } else {
	console.error("Element with id 'id_completed_appeal_text' not found or not html text area");
    }
}

function printAppeal() {
    console.log("Starting to print.")
    const childWindow = window.open('','_blank','');
    const completedAppealText = (document.getElementById("id_completed_appeal_text") as HTMLTextAreaElement)?.value || "";
    childWindow.document.open();
    childWindow.document.write('<html><head></head><body>');
    childWindow.document.write(completedAppealText.replace(/\n/gi,'<br>'));
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
    if (generate_button != null) {
	generate_button.onclick = async () => {
	    await generateAppealPDF();
	}
    }

    const print_button = document.getElementById('print_appeal');
    if (print_button != null) {
	print_button.onclick = async () => {
	    await printAppeal();
	}
    }

    const appeal_text = document.getElementById("scrubbed_appeal_text");
    if (appeal_text != null) {
	appeal_text.oninput = descrub
    }
    const descrub_button = document.getElementById('descrub');
    if (descrub_button != null) {
	descrub_button.onclick = descrub
    }
    descrub();
}


setupAppeal();
