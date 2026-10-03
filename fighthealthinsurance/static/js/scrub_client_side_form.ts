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

// The intake form's own messages, one under each field it checks
// (partials/field_error.html). Each is an empty live region until it is
// shown: showing it puts its words in, which is what a screen reader reads
// out, and marks the field it is about as invalid and described by it.
interface FieldCheck {
  // The id of the message under the field.
  message: string;
  // Every field the message is about.
  fields: HTMLElement[];
  // The ones that are not filled in yet.
  missing: HTMLElement[];
}

function hasDenialText(form: HTMLFormElement): boolean {
  return form.denial_text.value.trim().length > 0;
}

// Every check the form makes before it is sent, in page order, and exactly
// what the server requires (DenialForm in forms/__init__.py): the letter,
// the email and the four agreements.
function intakeChecks(form: HTMLFormElement): FieldCheck[] {
  const check = (
    message: string,
    fields: HTMLElement[],
    isMissing: (field: HTMLElement) => boolean,
  ): FieldCheck => ({ message, fields, missing: fields.filter(isMissing) });
  const unticked = (field: HTMLElement): boolean => !(field as HTMLInputElement).checked;
  return [
    check("need_denial", [form.denial_text], () => !hasDenialText(form)),
    check("email_error", [form.email], () => form.email.value.length < 1),
    check("pii_error", [form.pii], unticked),
    check("agree_chk_error", [form.privacy, form.tos, form.personalonly], unticked),
  ];
}

// Adds or takes out one id in a field's aria-describedby, leaving any other.
function describeBy(field: HTMLElement, id: string, on: boolean): void {
  const ids = (field.getAttribute("aria-describedby") ?? "")
    .split(/\s+/)
    .filter((token) => token !== "" && token !== id);
  if (on) {
    ids.push(id);
  }
  if (ids.length > 0) {
    field.setAttribute("aria-describedby", ids.join(" "));
  } else {
    field.removeAttribute("aria-describedby");
  }
}

// While a message shows, each field it is about that is still missing is
// invalid and described by it; the rest of its fields are neither.
function markFields(check: FieldCheck, shown: boolean): void {
  check.fields.forEach((field) => {
    const invalid = shown && check.missing.includes(field);
    if (invalid) {
      field.setAttribute("aria-invalid", "true");
    } else {
      field.removeAttribute("aria-invalid");
    }
    describeBy(field, check.message, invalid);
  });
}

function isShowing(check: FieldCheck): boolean {
  const message = document.getElementById(check.message);
  return message !== null && (message.textContent ?? "") !== "";
}

function showFieldMessage(check: FieldCheck): void {
  const message = document.getElementById(check.message);
  if (message) {
    const words = message.dataset.message ?? "";
    // A live region reads out a change, so words already showing are left
    // as they are rather than read out again.
    if (message.textContent !== words) {
      message.textContent = words;
    }
  }
  markFields(check, true);
}

function clearFieldMessage(check: FieldCheck): void {
  const message = document.getElementById(check.message);
  if (message) {
    message.textContent = "";
  }
  markFields(check, false);
}

// The first of these fields in page order takes focus and is brought into
// view, so a blocked submit shows the person where to look.
function focusFirst(fields: HTMLElement[]): void {
  let first: HTMLElement | null = null;
  for (const field of fields) {
    if (
      first === null ||
      first.compareDocumentPosition(field) & Node.DOCUMENT_POSITION_PRECEDING
    ) {
      first = field;
    }
  }
  if (first === null) {
    return;
  }
  first.focus({ preventScroll: true });
  first.scrollIntoView({ block: "center" });
}

// A page the server sent back marks each field it refused (scrub.html sets
// aria-invalid), and the first of them takes focus as on a blocked submit.
export function focusFirstRefusedField(form: HTMLFormElement): void {
  focusFirst(Array.from(form.querySelectorAll<HTMLElement>('[aria-invalid="true"]')));
}

export function hideErrorMessages(event: Event): void {
  const form = document.getElementById(
    "fuck_health_insurance_form",
  ) as HTMLFormElement | null;
  if (form == null) {
    return;
  }
  // Typing or ticking only takes messages down, or narrows one to the boxes
  // still unticked. A new message waits for the next submit.
  intakeChecks(form).forEach((check) => {
    if (!isShowing(check)) {
      return;
    }
    if (check.missing.length === 0) {
      clearFieldMessage(check);
    } else {
      markFields(check, true);
    }
  });
  if (hasDenialText(form)) {
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
//
// "In flight" means the LATEST file selection is being read, not "any batch is
// still running". Reading a batch is slow enough that a user can pick again
// while one is running, and scrub.ts drops every write from a superseded
// batch, so its text is never going to land. A plain counter got both
// directions wrong: a superseded batch finishing early hid the indicator for
// its successor, and a superseded batch still running kept the indicator up
// (and told the submit gate text was coming) after its successor had already
// reported failure (review).
let activeOcrSelection: number | null = null;

export function beginOcr(selection: number): void {
  activeOcrSelection = selection;
  // Show it as soon as reading starts, not just when the user hits submit.
  // Reading one photographed page takes several seconds and nothing else on
  // screen says so, so the page looked idle and people retyped their denial by
  // hand or gave up. The submit gate still shows this same box; it is now a
  // second entry point to a message that is already up rather than the only
  // way to ever see it.
  showHiddenMessage("ocr_in_progress");
}

export function endOcr(selection: number): void {
  if (selection !== activeOcrSelection) {
    // A superseded batch ending says nothing about the batch that replaced it.
    return;
  }
  activeOcrSelection = null;
  // The gate only re-evaluates on submit, so without this the "still
  // reading your file" message stays on screen after the file has
  // finished being read (external review).
  rehideHiddenMessage("ocr_in_progress");
}

export function isOcrInFlight(): boolean {
  return activeOcrSelection !== null;
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

export function validateScrubForm(event: Event): void {
  // Listener is bound to the <form>, so currentTarget is always the form
  const form = event.currentTarget as HTMLFormElement;
  // Each check shows or takes down its own message, and the same checks
  // decide below whether the form is sent, so a message never shows on a
  // form that goes anyway.
  const checks = intakeChecks(form);
  checks.forEach((check) => {
    if (check.missing.length > 0) {
      showFieldMessage(check);
    } else {
      clearFieldMessage(check);
    }
  });

  const denialTextReady = hasDenialText(form);
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
  } else if (!isOcrInFlight()) {
    // Only clear it once nothing is being read. With page one already in the
    // box, a submit blocked on some other field used to hide the indicator
    // while the remaining pages were still being read (review).
    rehideHiddenMessage("ocr_in_progress");
  }
  const missing = checks.flatMap((check) => check.missing);
  if (missing.length > 0) {
    // Not sent. The first field with a problem takes focus.
    event.preventDefault();
    focusFirst(missing);
    return;
  }
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

// Only an empty field takes what this browser kept. A field the page arrived
// with something in keeps it: the letter the server read from an upload
// (/server_side_ocr), the one it sends back with an error, or a microsite's
// starting line.
function retrieveFromLocalStorage(): void {
  FORM_FIELD_IDS.forEach((id) => {
    const element = document.getElementById(
      id,
    ) as HTMLInputElement | HTMLTextAreaElement | null;
    if (element && element.value === "") {
      const storedValue = getLocalStorageItemWithTTL(id);
      if (storedValue !== null) {
        element.value = storedValue;
      }
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
