import { getLocalStorageItemWithTTL, setLocalStorageItemWithTTL } from "./shared";

// Add text
export function addText(text: string): void {
  const input = document.getElementById("denial_text") as HTMLTextAreaElement;
  input.value += text;
}

// Error messages
function rehideHiddenMessage(name: string): void {
  const element = document.getElementById(name);
  if (element) {
    element.classList.remove("visible");
  }
}
function showHiddenMessage(name: string): void {
  const element = document.getElementById(name);
  if (element) {
    element.classList.add("visible");
  }
}
export function hideErrorMessages(event: Event): void {
  const form = document.getElementById(
    "fuck_health_insurance_form",
  ) as HTMLFormElement | null; 
  if (form == null) {
    return;
  }
  if (form.privacy.checked && form.personalonly.checked && form.tos.checked) {
    rehideHiddenMessage("agree_chk_error");
  }
  if (form.pii.checked) {
    rehideHiddenMessage("pii_error");
  }
  if (form.email.value.length > 1) {
    const emailLabel = document.getElementById("email-label");
    if (emailLabel) {
      emailLabel.style.color = "";
    }
    rehideHiddenMessage("email_error");
  }
  if (form.denial_text.value.trim().length > 0) {
    const denialTextLabel = document.getElementById("denial_text_label");
    if (denialTextLabel) {
      denialTextLabel.style.color = "";
    }
    rehideHiddenMessage("need_denial");
    // This runs on every keystroke in denial_text. Without clearing here,
    // "we couldn't read your file, so the box below is still empty" stayed on
    // screen while the user typed into that very box -- the gate only
    // re-evaluates on submit, so nothing else would have taken it down.
    rehideHiddenMessage("ocr_failed");
    rehideHiddenMessage("ocr_partial");
  }
}
// OCR runs asynchronously after a file is chosen, and for a scanned PDF it
// renders every page and runs tesseract -- seconds to minutes. Nothing used to
// stop the user submitting during that window, so they got a server-side
// "denial_text: This field is required" while the page was visibly still
// working. Track it so the submit gate can say "still reading your file"
// instead.
let ocrInFlight = 0;

export function beginOcr(): void {
  ocrInFlight += 1;
}

export function endOcr(): void {
  ocrInFlight = Math.max(0, ocrInFlight - 1);
  if (ocrInFlight === 0) {
    // The gate only re-evaluates on submit, so without this the "still
    // reading your file" message stays on screen after the file has
    // finished being read (external review).
    rehideHiddenMessage("ocr_in_progress");
  }
}

export function isOcrInFlight(): boolean {
  return ocrInFlight > 0;
}

// Reading the file can fail outright (every OCR engine erroring or timing
// out) or "succeed" while producing nothing usable -- a blurry photo, a
// scan too faint for tesseract. Both used to be SILENT: recognize() threw
// into a handler that had a finally and no catch, so the textarea simply
// stayed empty and the user got "we need your denial text" underneath copy
// promising they could upload a file instead. Tell them what happened.
export function noteOcrFailure(): void {
  rehideHiddenMessage("ocr_partial");
  showHiddenMessage("ocr_failed");
}

// Some pages read, some did not. Distinct from total failure on purpose: the
// total-failure copy says the box is still empty, which is plainly false when
// the rest of the batch just filled it.
export function notePartialOcrFailure(): void {
  rehideHiddenMessage("ocr_failed");
  showHiddenMessage("ocr_partial");
}

export function clearOcrFailure(): void {
  rehideHiddenMessage("ocr_failed");
  rehideHiddenMessage("ocr_partial");
}

// The length of what we have so far, so the caller can tell "the engines
// ran without throwing" from "the engines actually produced text".
export function denialTextLength(): number {
  const input = document.getElementById("denial_text") as HTMLTextAreaElement | null;
  return input ? input.value.trim().length : 0;
}

export function validateScrubForm(event: Event): void {
  // Listener is bound to the <form>, so currentTarget is always the form
  const form = event.currentTarget as HTMLFormElement;
  if (
    !form.privacy.checked ||
    !form.personalonly.checked ||
    !form.tos.checked
  ) {
    showHiddenMessage("agree_chk_error");
  } else {
    rehideHiddenMessage("agree_chk_error");
  }
  if (!form.pii.checked) {
    showHiddenMessage("pii_error");
  } else {
    rehideHiddenMessage("pii_error");
  }
  if (form.email.value.length < 1) {
    showHiddenMessage("email_error");
    const emailLabel = document.getElementById("email-label");
    if (emailLabel) {
      emailLabel.style.color = "red";
    }
  } else {
    const emailLabel = document.getElementById("email-label");
    if (emailLabel) {
      emailLabel.style.color = "";
    }
    rehideHiddenMessage("email_error");
  }
  if (form.denial_text.value.trim().length < 1) {
    showHiddenMessage("need_denial");
    const denialTextLabel = document.getElementById("denial_text_label");
    if (denialTextLabel) {
      denialTextLabel.style.color = "red";
    }
  } else {
    const denialTextLabel = document.getElementById("denial_text_label");
    if (denialTextLabel) {
      denialTextLabel.style.color = "";
    }
    rehideHiddenMessage("need_denial");
  }

  // Every field validated above must also GATE the submit. This condition
  // used to check only pii/privacy/email, so a form with an empty
  // denial_text displayed "need_denial" and then submitted regardless --
  // the server rejected it with "denial_text: This field is required" and
  // the user saw a contradiction. personalonly and tos were validated and
  // ungated the same way.
  const denialTextReady = form.denial_text.value.trim().length > 0;
  if (denialTextReady) {
    // However the text arrived -- a later file that read fine, or the user
    // pasting it -- the earlier failure is no longer something to act on.
    rehideHiddenMessage("ocr_failed");
    rehideHiddenMessage("ocr_partial");
  }
  if (!denialTextReady && isOcrInFlight()) {
    // Distinguish "you have not given us the letter" from "we are still
    // reading the file you just gave us".
    showHiddenMessage("ocr_in_progress");
  } else {
    rehideHiddenMessage("ocr_in_progress");
  }
  // Gate on exactly what the SERVER requires: forms/__init__.py marks pii,
  // tos and privacy required=True, plus email and denial_text. personalonly
  // is deliberately NOT here -- it is an optional checkbox that the
  // agree_chk_error branch above happens to mention, and gating on it made
  // the client stricter than the server, blocking a submission the server
  // would have accepted (caught by the Selenium suite).
  if (
    form.pii.checked &&
    form.privacy.checked &&
    form.tos.checked &&
    form.email.value.length > 0 &&
    denialTextReady
  ) {
    rehideHiddenMessage("agree_chk_error");
    rehideHiddenMessage("pii_error");
    rehideHiddenMessage("email_error");
    rehideHiddenMessage("need_denial");
    // Only include fname and lname if user has subscribed to mailing list
    // This ensures we don't send personal names to the server unless the user opts in
    // Remove any previously added hidden inputs to prevent duplicates
    const existingFname = form.querySelector('input[type="hidden"][name="fname"]');
    const existingLname = form.querySelector('input[type="hidden"][name="lname"]');
    if (existingFname) {
      existingFname.remove();
    }
    if (existingLname) {
      existingLname.remove();
    }
    if (form.subscribe.checked) {
      // Get the locally stored fname/lname values
      const fnameInput = document.getElementById(
        "store_fname",
      ) as HTMLInputElement | null;
      const lnameInput = document.getElementById(
        "store_lname",
      ) as HTMLInputElement | null;
      // Add hidden inputs to the form to send fname and lname to the server
      if (fnameInput && fnameInput.value) {
        const hiddenFname = document.createElement("input");
        hiddenFname.type = "hidden";
        hiddenFname.name = "fname";
        hiddenFname.value = fnameInput.value;
        form.appendChild(hiddenFname);
      }
      if (lnameInput && lnameInput.value) {
        const hiddenLname = document.createElement("input");
        hiddenLname.type = "hidden";
        hiddenLname.name = "lname";
        hiddenLname.value = lnameInput.value;
        form.appendChild(hiddenLname);
      }
    }
    // YOLO
    return;
  } else {
    // Bad news no submit
    event.preventDefault();
  }
}

// Shared ID array for DRY principle
const FORM_FIELD_IDS = [
  "fname",
  "lname",
  "dob",
  "email_address",
  "subscriber_id",
  "group_id",
  "plan_name",
  "insurance_company",
  "claim_id",
  "date_of_service",
  "denial_reason",
  "denial_text",
  "notes",
];

function storeInLocalStorage(): void {
  FORM_FIELD_IDS.forEach((id) => {
    const element = document.getElementById(
      id,
    ) as HTMLInputElement | HTMLTextAreaElement | null;
    if (element) {
      setLocalStorageItemWithTTL(id, element.value);
    }
  });
}

function retrieveFromLocalStorage(): void {
  FORM_FIELD_IDS.forEach((id) => {
    const element = document.getElementById(
      id,
    ) as HTMLInputElement | HTMLTextAreaElement | null;
    if (element) {
      const storedValue = getLocalStorageItemWithTTL(id);
      element.value = storedValue !== null ? storedValue : "";
    }
  });
}

function toggleSection(name: string): void {
  const element = document.getElementById(name);
  if (element) {
    if (element.classList.contains("visible")) {
      element.classList.remove("visible");
    } else {
      element.classList.add("visible");
    }
  }
}

function validateAndStore(): boolean {
  let isValid = true;
  const emailElement = document.getElementById(
    "email_address",
  ) as HTMLInputElement | null;
  const denialTextElement = document.getElementById(
    "denial_text",
  ) as HTMLTextAreaElement | null;
  const emailLabel = document.getElementById("email-label");
  const denialTextLabel = document.getElementById("denial_text_label");

  if (emailElement && emailLabel) {
    if (emailElement.value === "" || !emailElement.validity.valid) {
      emailLabel.style.color = "red";
      isValid = false;
    } else {
      emailLabel.style.color = "";
    }
  } else {
    isValid = false; // Element not found, consider it invalid
  }

  if (denialTextElement && denialTextLabel) {
    if (denialTextElement.value === "") {
      denialTextLabel.style.color = "red";
      isValid = false;
    } else {
      denialTextLabel.style.color = "";
    }
  } else {
    isValid = false; // Element not found
  }

  if (isValid) {
    storeInLocalStorage();
    alert("Information Stored in Local Storage");
  } else {
    alert(
      "Please fill out all required fields correctly (Email and Denial Text).",
    );
  }
  return isValid;
}

document.addEventListener("DOMContentLoaded", (event) => {
  retrieveFromLocalStorage();
  const form = document.getElementById("scrubform") as HTMLFormElement | null;
  if (form) {
    form.addEventListener("submit", (event) => {
      event.preventDefault(); // stop form from submitting
      validateAndStore();
    });
  }

  const storeButton = document.getElementById(
    "storeButton",
  ) as HTMLButtonElement | null;
  if (storeButton) {
    storeButton.addEventListener("click", () => {
      validateAndStore();
    });
  }

  const toggleButtons = document.querySelectorAll(".toggle-button");
  toggleButtons.forEach((button) => {
    button.addEventListener("click", () => {
      const sectionName = button.getAttribute("data-section");
      if (sectionName) {
        toggleSection(sectionName);
      }
    });
  });
});

export {
  storeInLocalStorage,
  retrieveFromLocalStorage,
  toggleSection,
  validateAndStore,
};
