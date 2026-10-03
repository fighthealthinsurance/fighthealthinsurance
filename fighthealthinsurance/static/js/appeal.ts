import { jsPDF, jsPDFOptions } from "jspdf";

import {
  getLocalStorageItemOrDefault,
  getLocalStorageItemOrDefaultEQ,
  setLocalStorageItemWithTTL,
} from "./shared";
import {
  faxMustWaitForPlaceholders,
  printUnlessPlaceholders,
} from "./letter_placeholders";
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

// True once the person has typed in the finished letter themselves. Their
// words win from then on: the letter is rebuilt from the draft and the
// details panel only when they ask for it with the rebuild button, never
// silently underneath them, and never on the way to the fax.
let completedLetterEdited = false;
let rebuildingCompletedLetter = false;

function noteCompletedLetterEdit(): void {
  if (!rebuildingCompletedLetter) {
    completedLetterEdited = true;
  }
}

// `force` is the rebuild button and the panel edits the person just made:
// an explicit ask. Everything else leaves a hand-edited letter alone.
function descrub(force = false) {
  if (completedLetterEdited && !force) {
    return;
  }
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

  // Fuzzy matching for model-generated placeholder variants
  // like [Claim # Placeholder], [Patient Name Placeholder], etc.
  const fuzzyReplacements: [RegExp, string][] = [
    [/\[Claim\s*#?\s*(?:Number\s*)?(?:Placeholder)?\]/gi, claim_id],
    [/\[Reference\s*#?\s*(?:Number\s*)?(?:Placeholder)?\]/gi, claim_id],
    [/\[Patient(?:'?s?)?\s+Name\s*(?:Placeholder)?\]/gi, name],
    [/\[Subscriber\s*(?:ID|#)\s*(?:Placeholder)?\]/gi, subscriber_id],
    [/\[Group\s*(?:ID|#)\s*(?:Placeholder)?\]/gi, group_id],
    [/\[Your\s+Name\s*(?:Placeholder)?\]/gi, name],
    [/\[(?:Email|Email\s+Address)\s*(?:Placeholder)?\]/gi, email_address],
    [/\[Phone\s*(?:Number)?\s*(?:Placeholder)?\]/gi, phone_number],
  ];
  // Sentinel defaults that indicate no real value was stored
  const sentinels = new Set([
    "FirstName", "LastName", "FirstName LastName",
    "subscriber_id", "group_id", "claim_id",
    "email_address", "phone_number",
  ]);
  for (const [pattern, value] of fuzzyReplacements) {
    if (value && value !== "" && !sentinels.has(value)) {
      text = text.replace(pattern, () => value);
    }
  }

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
    rebuildingCompletedLetter = true;
    try {
      target.value = text;
    } finally {
      rebuildingCompletedLetter = false;
    }
    // A rebuild is the letter the person asked for, so it is no longer
    // an edit of theirs waiting to be protected.
    completedLetterEdited = false;
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
    // The letter is typed by the person and written by a model, so it is
    // text, not markup: it goes in through textContent, and the only thing
    // written as HTML is this fixed shell. A pre with wrapping keeps the
    // line breaks a letter needs while printing in the page's own font.
    const doc = childWindow.document;
    doc.open();
    doc.write(
      "<!doctype html><html><head><title>Your appeal</title>" +
        "<style>body{margin:1in;font-family:inherit}" +
        "pre{white-space:pre-wrap;word-wrap:break-word;font:inherit;margin:0}</style>" +
        "</head><body></body></html>",
    );
    doc.close();
    const letter = doc.createElement("pre");
    letter.textContent = completedAppealText;
    doc.body.appendChild(letter);
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
      // Re-run descrub so the completed appeal textarea reflects the new value.
      // The person is editing their own details here, so this is an ask.
      descrub(true);
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

  // Print checks the letter for blanks first. Paper can be filled in by
  // hand, so the notice offers to print anyway.
  const print_button = document.getElementById("print_appeal");
  if (print_button != null) {
    print_button.onclick = () => {
      printUnlessPlaceholders(
        print_button,
        document.getElementById("id_completed_appeal_text") as HTMLTextAreaElement | null,
        printAppeal,
      );
    };
  }

  // Populate PII panel from localStorage and wire up save-on-change
  populatePiiPanel();
  setupPiiPanelListeners();

  const appeal_text = document.getElementById("scrubbed_appeal_text");
  if (appeal_text != null) {
    // Wrapped, not assigned: as a handler the event object would arrive as
    // `force` and rebuild the letter on every keystroke in the draft.
    appeal_text.oninput = () => descrub();
  }
  const descrub_button = document.getElementById("descrub");
  if (descrub_button != null) {
    // The one control whose whole purpose is to rebuild the letter.
    descrub_button.onclick = () => descrub(true);
  }
  const completed_text = document.getElementById("id_completed_appeal_text");
  if (completed_text != null) {
    completed_text.addEventListener("input", noteCompletedLetterEdit);
  }
  // On a server rejection the page comes back with the letter the person
  // submitted already in the box (fax_views puts the posted text in the
  // context). Rebuilding on load would strip everything they had added
  // after the details block, so the first build only fills an empty box
  // (review).
  const completedOnLoad = (completed_text as HTMLTextAreaElement | null)?.value ?? "";
  if (completedOnLoad.trim() === "") {
    descrub();
  } else {
    completedLetterEdited = true;
  }

  // A fax waits until the letter has no blanks left in it. The server
  // refuses the same letter (FaxForm), so this check only saves the round
  // trip and says which blanks they are, just above the button.
  const faxButton = document.getElementById("fax_appeal");
  const faxForm = faxButton?.closest("form") as HTMLFormElement | null;
  if (faxButton && faxForm) {
    faxForm.addEventListener("submit", (e) => {
      // Pick up the details panel for a letter the person has not touched,
      // and leave a hand-edited letter exactly as they left it: this runs
      // one line before the text is read and posted.
      descrub();
      const letter = document.getElementById(
        "id_completed_appeal_text",
      ) as HTMLTextAreaElement | null;
      const appealText = letter?.value || "";
      if (appealText.trim() === "") {
        // An empty letter would go out as an empty fax.
        e.preventDefault();
        alert("There is no letter to send. Write or rebuild your letter first.");
        return;
      }
      // Decided inside this one submission: a letter with no blanks goes
      // straight through, and the next press is checked afresh. Nothing
      // re-submits the form from inside its own event.
      if (faxMustWaitForPlaceholders(faxButton, letter)) {
        e.preventDefault();
      }
    });
  }
}

setupAppeal();
