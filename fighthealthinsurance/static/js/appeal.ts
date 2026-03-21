import { jsPDF, jsPDFOptions } from "jspdf";

import {
  getLocalStorageItemOrDefault,
  getLocalStorageItemOrDefaultEQ,
  setLocalStorageItemWithTTL,
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

// Read a PII panel input value, falling back to localStorage then a default
function getPiiValue(inputId: string, storageKey: string, defaultVal: string): string {
  const el = document.getElementById(inputId) as HTMLInputElement | null;
  if (el && el.value.trim()) {
    return el.value.trim();
  }
  return getLocalStorageItemOrDefault(storageKey, defaultVal);
}

// Sentinel that marks the start of the appended PII block so we can strip & re-add
const PII_BLOCK_MARKER = "\n\n---\nPatient Information:";

function descrub() {
  const appeal_text = document.getElementById("scrubbed_appeal_text");
  const target = document.getElementById(
    "id_completed_appeal_text",
  ) as HTMLTextAreaElement;
  var text = (appeal_text as HTMLTextAreaElement)?.value || "";

  // Read from PII panel inputs first, fall back to localStorage
  const fname = getPiiValue("pii_fname", "store_fname", "FirstName");
  const lname = getPiiValue("pii_lname", "store_lname", "LastName");
  const subscriber_id = getPiiValue("pii_subscriber_id", "subscriber_id", "subscriber_id");
  const group_id = getPiiValue("pii_group_id", "group_id", "group_id");
  const claim_id = getLocalStorageItemOrDefaultEQ("claim_id");
  const email_address = getPiiValue("pii_email", "email_address", "email_address");
  const phone_number = getPiiValue("pii_phone", "phone_number", "phone_number");
  const street = getPiiValue("pii_street", "store_street", "");
  const city = getPiiValue("pii_city", "store_city", "");
  const state = getPiiValue("pii_state", "store_state", "");
  const zip = getPiiValue("pii_zip", "store_zip", "");
  const name = [fname, lname].filter(Boolean).join(" ");

  // Build UserInfo and use restorePersonalInfo for primary {{PLACEHOLDER}}
  // and legacy [BRACKET] replacements
  const userInfo: UserInfo = {
    firstName: fname,
    lastName: lname,
    email: email_address,
    address: street,
    city: city,
    state: state,
    zipCode: zip,
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

  // Strip any previously appended PII block before re-adding
  const markerIdx = text.indexOf(PII_BLOCK_MARKER);
  if (markerIdx !== -1) {
    text = text.substring(0, markerIdx);
  }

  // Build and append PII summary block at the bottom of the letter
  const isReal = (val: string, ...defaults: string[]) =>
    val && !defaults.includes(val);

  const lines: string[] = [];
  if (isReal(name, "FirstName", "LastName", "FirstName LastName"))
    lines.push(`Name: ${name}`);
  const addrParts = [street, city, state, zip].filter(Boolean);
  if (addrParts.length > 0) {
    // Format as "Street, City, State Zip"
    let addr = street;
    if (city) addr += (addr ? ", " : "") + city;
    if (state) addr += (addr ? ", " : "") + state;
    if (zip) addr += (addr ? " " : "") + zip;
    lines.push(`Address: ${addr}`);
  }
  if (isReal(phone_number, "phone_number"))
    lines.push(`Phone: ${phone_number}`);
  if (isReal(email_address, "email_address"))
    lines.push(`Email: ${email_address}`);
  if (isReal(subscriber_id, "subscriber_id"))
    lines.push(`Subscriber ID: ${subscriber_id}`);
  if (isReal(group_id, "group_id"))
    lines.push(`Group ID: ${group_id}`);
  if (isReal(claim_id, "claim_id"))
    lines.push(`Claim ID: ${claim_id}`);

  if (lines.length > 0) {
    text += PII_BLOCK_MARKER + "\n" + lines.join("\n");
  }

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

  // Generic bracket placeholders like [Diagnosis], [Patient's Name], etc.
  // Matches [Title Case...] or [UPPER CASE...] to avoid false positives
  // on normal bracketed text like [1] or [see above].
  const bracketMatches = text.match(/\[[A-Z][A-Za-z' ]+\]/g);
  if (bracketMatches) {
    found.push(...bracketMatches);
  }

  // Single-brace placeholders like {diagnosis}, {insurance_company}
  // Filter out {{double_brace}} matches without lookbehind (unsupported in Safari <16.4)
  const singleBraceRe = /\{[a-z_]+\}/g;
  let singleMatch: RegExpExecArray | null;
  while ((singleMatch = singleBraceRe.exec(text)) !== null) {
    const idx = singleMatch.index;
    const end = idx + singleMatch[0].length;
    // Skip if this is part of a {{...}} double-brace placeholder
    if (
      (idx > 0 && text[idx - 1] === "{") ||
      (end < text.length && text[end] === "}")
    ) {
      continue;
    }
    found.push(singleMatch[0]);
  }

  // Dollar-prefixed template variables like $diagnosis, $insurance_company
  const dollarMatches = text.match(/\$[a-z_]+/g);
  if (dollarMatches) {
    found.push(...dollarMatches);
  }

  // Default placeholder values that indicate unfilled fields
  const defaultPlaceholders: [RegExp, string][] = [
    [/\bFirstName\b/, "FirstName"],
    [/\bLastName\b/, "LastName"],
    [/\bYourNameMagic\b/, "YourNameMagic"],
    // Sentinel fallback literals from descrub() when no real value is stored
    [/\bsubscriber_id\b/, "subscriber_id"],
    [/\bgroup_id\b/, "group_id"],
    [/\bclaim_id\b/, "claim_id"],
    [/\bphone_number\b/, "phone_number"],
    [/\bemail_address\b/, "email_address"],
  ];
  for (const [regex, label] of defaultPlaceholders) {
    if (regex.test(text)) {
      found.push(label);
    }
  }

  // Deduplicate so the confirm dialog isn't noisy
  return Array.from(new Set(found));
}

// Mapping from PII panel input IDs to localStorage keys
const PII_FIELD_MAP: [string, string][] = [
  ["pii_fname", "store_fname"],
  ["pii_lname", "store_lname"],
  ["pii_phone", "phone_number"],
  ["pii_email", "email_address"],
  ["pii_street", "store_street"],
  ["pii_city", "store_city"],
  ["pii_state", "store_state"],
  ["pii_zip", "store_zip"],
  ["pii_subscriber_id", "subscriber_id"],
  ["pii_group_id", "group_id"],
];

function populatePiiPanel() {
  for (const [inputId, storageKey] of PII_FIELD_MAP) {
    const el = document.getElementById(inputId) as HTMLInputElement | null;
    if (!el) continue;
    const stored = getLocalStorageItemOrDefault(storageKey, "");
    // Only populate if the stored value is a real value (not the key itself)
    if (stored && stored !== storageKey) {
      el.value = stored;
    }
  }
}

function setupPiiPanelListeners() {
  for (const [inputId, storageKey] of PII_FIELD_MAP) {
    const el = document.getElementById(inputId) as HTMLInputElement | null;
    if (!el) continue;
    el.addEventListener("input", () => {
      setLocalStorageItemWithTTL(storageKey, el.value);
      // Re-run descrub so the completed appeal textarea reflects the new value
      descrub();
    });
  }
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

  // Populate PII panel from localStorage and wire up save-on-change
  populatePiiPanel();
  setupPiiPanelListeners();

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
      // Regenerate appeal text from current PII panel values before checking
      descrub();
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
          "\n\nYou may need to fill in your PII/PHI manually — please double-check the letter before submission.\n\n" +
          "Press OK to send the fax anyway, or Cancel to go back and fill in your information first."
        );
        if (proceed) {
          skipCheck = true;
          // Use requestSubmit() so other submit handlers (e.g. pwyw tracking)
          // still fire and HTML5 constraint validation runs. skipCheck prevents
          // this handler from re-triggering the placeholder check.
          faxForm.requestSubmit();
        }
      }
    });
  }
}

setupAppeal();
