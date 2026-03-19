import { jsPDF, jsPDFOptions } from "jspdf";

import {
  getLocalStorageItemOrDefault,
  getLocalStorageItemOrDefaultEQ,
} from "./shared";
import { restorePersonalInfo, type UserInfo } from "./user_info_storage";

async function generateAppealPDF() {
  const options: jsPDFOptions = {
    orientation: "p", // portrait
    unit: "px",
    format: "letter",
  };

  const completedAppealText =
    (document.getElementById("id_completed_appeal_text") as HTMLTextAreaElement)
      ?.value || "";

  // Create a new jsPDF document
  const doc = new jsPDF(options);

  // Add the text box contents to the PDF document
  doc.text(completedAppealText, 20, 20, { maxWidth: 300 });

  doc.setProperties({
    title: "Health Insurance Appeal",
  });

  // Save the PDF document and download it
  doc.save("appeal.pdf");
}

function descrub() {
  const appeal_text = document.getElementById("scrubbed_appeal_text");
  const target = document.getElementById(
    "id_completed_appeal_text",
  ) as HTMLTextAreaElement;
  var text = (appeal_text as HTMLTextAreaElement)?.value || "";
  const fname = getLocalStorageItemOrDefault("store_fname", "FirstName");
  const lname = getLocalStorageItemOrDefault("store_lname", "LastName");
  const subscriber_id = getLocalStorageItemOrDefaultEQ("subscriber_id");
  const group_id = getLocalStorageItemOrDefaultEQ("group_id");
  const claim_id = getLocalStorageItemOrDefaultEQ("claim_id");
  const email_address = getLocalStorageItemOrDefaultEQ("email_address");
  const phone_number = getLocalStorageItemOrDefaultEQ("phone_number");
  const name = [fname, lname].filter(Boolean).join(" ");

  // Build UserInfo from localStorage and use restorePersonalInfo for
  // primary {{PLACEHOLDER}} and legacy [BRACKET] replacements
  const userInfo: UserInfo = {
    firstName: fname,
    lastName: lname,
    email: email_address,
    address: getLocalStorageItemOrDefault("store_street", ""),
    city: getLocalStorageItemOrDefault("store_city", ""),
    state: getLocalStorageItemOrDefault("store_state", ""),
    zipCode: getLocalStorageItemOrDefault("store_zip", ""),
    acceptedTerms: true,
  };
  text = restorePersonalInfo(text, userInfo);

  // Additional {{PLACEHOLDER}} formats not covered by restorePersonalInfo
  text = text.replace(/\{\{Your Name\}\}/g, name);
  text = text.replace(/\{\{SCSID\}\}/g, subscriber_id);
  text = text.replace(/\{\{GPID\}\}/g, group_id);
  text = text.replace(/\{\{CASEID\}\}/g, claim_id);
  text = text.replace(/\{\{Your Phone Number\}\}/g, phone_number);

  // Legacy format fallbacks for backward compatibility
  text = text.replace(/YourNameMagic/g, name);
  text = text.replace(/\[Patient's Name\]/g, name);
  text = text.replace(/\[Policy Number or Member ID\]/g, subscriber_id);
  text = text.replace(/SCSID: 123456789/g, subscriber_id);
  text = text.replace(/GPID: 987654321/g, group_id);
  text = text.replace(/subscriber\\_id/g, subscriber_id);
  text = text.replace(/group\\_id/g, group_id);
  // These must come after the more specific patterns above
  text = text.replace(/subscriber_id/g, subscriber_id);
  text = text.replace(/group_id/g, group_id);
  text = text.replace(/\bfname\b/g, fname);
  text = text.replace(/\blname\b/g, lname);
  if (target) {
    target.value = text;
  } else {
    console.error(
      "Element with id 'id_completed_appeal_text' not found or not html text area",
    );
  }
}

function printAppeal() {
  console.log("Starting to print.");
  const childWindow = window.open("", "_blank", "");
  const completedAppealText =
    (document.getElementById("id_completed_appeal_text") as HTMLTextAreaElement)
      ?.value || "";

  if (childWindow) {
    childWindow.document.open();
    childWindow.document.write("<html><head></head><body>");
    childWindow.document.write(completedAppealText.replace(/\n/gi, "<br>"));
    childWindow.document.write("</body></html>");
    // Wait 1 second for chrome.
    setTimeout(function () {
      console.log("Executed after 1 second");
      if (childWindow && typeof childWindow.print === "function") {
        childWindow.print();
      }
    }, 1000);
    console.log("Done!");
    //    childWindow.document.close();
    //    childWindow.close();
  } else {
    console.error(
      "Failed to open print window. It might have been blocked by a popup blocker.",
    );
    alert(
      "Failed to open print window. Please check your popup blocker settings.",
    );
  }
}

function checkForUnfilledPlaceholders(text: string): string[] {
  const found: string[] = [];

  // Double-brace placeholders like {{FIRST_NAME}}, {{Your Name}}, etc.
  const braceMatches = text.match(/\{\{[^}]+\}\}/g);
  if (braceMatches) {
    found.push(...braceMatches);
  }

  // Legacy bracket placeholders
  for (const pattern of ["[Patient's Name]", "[Policy Number or Member ID]"]) {
    if (text.includes(pattern)) {
      found.push(pattern);
    }
  }

  // Default placeholder values that indicate unfilled fields
  const defaultPlaceholders: [RegExp, string][] = [
    [/\bFirstName\b/, "FirstName"],
    [/\bLastName\b/, "LastName"],
    [/\bYourNameMagic\b/, "YourNameMagic"],
  ];
  for (const [regex, label] of defaultPlaceholders) {
    if (regex.test(text)) {
      found.push(label);
    }
  }

  return found;
}

function setupAppeal() {
  const generate_button = document.getElementById("generate_pdf");
  if (generate_button != null) {
    generate_button.onclick = async () => {
      await generateAppealPDF();
    };
  }

  const print_button = document.getElementById("print_appeal");
  if (print_button != null) {
    print_button.onclick = async () => {
      await printAppeal();
    };
  }

  const appeal_text = document.getElementById("scrubbed_appeal_text");
  if (appeal_text != null) {
    appeal_text.oninput = descrub;
  }
  const descrub_button = document.getElementById("descrub");
  if (descrub_button != null) {
    descrub_button.onclick = descrub;
  }
  descrub();

  // Warn before fax submission if PHI placeholders remain unfilled
  const faxButton = document.getElementById("fax_appeal");
  const faxForm = faxButton?.closest("form") as HTMLFormElement | null;
  if (faxForm) {
    let skipCheck = false;
    faxForm.addEventListener("submit", (e) => {
      if (skipCheck) {
        skipCheck = false;
        return;
      }
      const appealText =
        (document.getElementById("id_completed_appeal_text") as HTMLTextAreaElement)
          ?.value || "";
      const placeholders = checkForUnfilledPlaceholders(appealText);
      if (placeholders.length > 0) {
        e.preventDefault();
        const listing = placeholders.join(", ");
        const proceed = confirm(
          "Your appeal still contains placeholder text that should be replaced with your personal information:\n\n" +
          listing +
          "\n\nYou may need to fill in your PII/PHI manually — please double-check the letter before submission.\n" +
          "Press OK to auto-fill your PII, or Cancel to edit the appeal manually.\n\n" +
          "Do you want to send the fax anyway?"
        );
        if (proceed) {
          skipCheck = true;
          // Use submit() intentionally (not requestSubmit()) to bypass the
          // submit event handler and avoid re-triggering the placeholder check.
          // Server-side validation is handled by StageFaxView.form_valid().
          faxForm.submit();
        }
      }
    });
  }
}

setupAppeal();
