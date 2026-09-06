import { storeLocal, storeTextareaLocal, getLocalStorageItemWithTTL, setLocalStorageItemWithTTL, isPersistenceEnabled, setPersistenceEnabled } from "./shared";

import { recognize } from "./scrub_ocr";

import { clean } from "./scrub_scrub";

import {
  addText,
  beginOcr,
  clearOcrFailure,
  denialTextLength,
  endOcr,
  hideErrorMessages,
  notePartialOcrFailure,
  noteOcrFailure,
  validateScrubForm,
} from "./scrub_client_side_form";

import { detectWebGPUAvailability } from "./qwen_webgpu_ocr";

// Network Information API (not yet in standard TypeScript lib types).
interface NetworkInformation {
  type?: string;
  effectiveType?: string;
  saveData?: boolean;
}

function isLikelyMobileOrMeteredNetwork(): boolean {
  const connection = (navigator as Navigator & { connection?: NetworkInformation }).connection;
  if (!connection) return false;
  return (
    connection.saveData === true ||
    connection.type === "cellular" ||
    connection.effectiveType === "2g" ||
    connection.effectiveType === "slow-2g"
  );
}

async function initAdvancedOCRCheckbox(): Promise<void> {
  const checkbox = document.getElementById("advanced_ocr_enabled") as HTMLInputElement | null;
  if (!checkbox) return;

  // Disable by default on metered/mobile connections.
  if (isLikelyMobileOrMeteredNetwork()) {
    checkbox.checked = false;
    return;
  }

  // Disable by default when WebGPU is unavailable.
  const webGpu = await detectWebGPUAvailability();
  if (!webGpu.available) {
    checkbox.checked = false;
  }
}

// Which selection is current. Reading a batch is slow enough that a user can
// pick again while one is still running, and only the newest pick describes
// what is on screen: without this, a slow FAILING batch could finish after a
// newer successful one and re-post "we couldn't read your file" over text
// that had just arrived.
let latestOcrSelection = 0;

const recognizeEvent = async function (evt: Event) {
  const input = evt.target as HTMLInputElement;
  const files = input.files;

  if (!files) {
    return; // Exit early if files is null
  }

  const filesArray = Array.from(files);
  const selection = ++latestOcrSelection;

  // Mark OCR as in flight for the whole batch so the submit gate can tell the
  // user we are still reading their file rather than letting them submit an
  // empty denial_text. endOcr() must run even when recognize() throws.
  //
  // A fresh selection replaces the previous verdict: the last attempt having
  // failed says nothing about this one.
  clearOcrFailure();
  const before = denialTextLength();
  let failures = 0;

  beginOcr();
  try {
    for (const file of filesArray) {
      // Catch PER FILE, not around the loop. The uploader is multiple="true"
      // and people attach a denial one page per image; with a single catch
      // outside, one unreadable page abandoned every page after it and the
      // user was never told which -- or that anything had gone wrong at all.
      try {
        await recognize(file, addText);
      } catch (error) {
        failures += 1;
        // The filename can carry the patient's name; keep it out of logs.
        console.error("OCR failed for an uploaded file:", error);
      }
    }
  } finally {
    endOcr();
  }

  // A superseded batch says nothing about what the user is looking at.
  if (selection !== latestOcrSelection) {
    return;
  }

  // "Threw" is not the only failure. Every engine can return cleanly and
  // still yield nothing for a photo too blurry to read, which looks
  // identical to the user: an empty box under a form that says a file is
  // enough. Treat "no new text" as a failure too.
  const gainedText = denialTextLength() > before;
  if (failures > 0 && gainedText) {
    // Some pages read and some did not. Saying "we couldn't read your file"
    // here would be plainly false with their text sitting right below it.
    notePartialOcrFailure();
  } else if (failures > 0 || !gainedText) {
    noteOcrFailure();
  }
};

function setupScrub(): void {
  // Setup persistence toggle checkbox
  const persistenceCheckbox = document.getElementById("persistence_enabled") as HTMLInputElement;
  if (persistenceCheckbox) {
    persistenceCheckbox.checked = isPersistenceEnabled();
    persistenceCheckbox.addEventListener("change", (event) => {
      const target = event.target as HTMLInputElement;
      setPersistenceEnabled(target.checked);
    });
  }

  // Auto-detect capabilities and update the advanced OCR checkbox default.
  initAdvancedOCRCheckbox().catch((err) => {
    console.warn("[AdvancedOCR] Could not determine default state:", err);
  });

  // Restore previous local values
  // Don't log the nodes themselves: the inputs hold name/address/email values.
  var nodes: NodeListOf<HTMLInputElement> = document.querySelectorAll("input");
  console.debug("scrub: input nodes found", nodes.length);
  function handleStorage(node: HTMLInputElement) {
    // All store_ fields which are local only and the e-mail field which is local and non-local.
    if (node.id.startsWith("store_") || node.id.startsWith("email")) {
      node.addEventListener("change", storeLocal);
      if (node.value == "") {
        const storedValue = getLocalStorageItemWithTTL(node.id);
        node.value = storedValue !== null ? storedValue : ""; // Ensure string assignment
      }
    }
  }
  nodes.forEach(handleStorage);

  // Handle textareas (for denial_text)
  const textareas: NodeListOf<HTMLTextAreaElement> = document.querySelectorAll("textarea");
  textareas.forEach((textarea) => {
    if (textarea.id === "denial_text" || textarea.id.startsWith("store_")) {
      textarea.addEventListener("input", storeTextareaLocal);
      if (textarea.value === "") {
        const storedValue = getLocalStorageItemWithTTL(textarea.id);
        textarea.value = storedValue !== null ? storedValue : "";
      }
    }
  });

  const elm = document.getElementById("uploader");
  if (elm != null) {
    elm.addEventListener("change", recognizeEvent);
  }
  const scrub = document.getElementById("scrub");
  if (scrub != null) {
    scrub.onclick = clean;
  }
  const scrub2 = document.getElementById("scrub-2");
  if (scrub2 != null) {
    scrub2.onclick = clean;
  }
  const form = document.getElementById(
    "fuck_health_insurance_form",
  ) as HTMLFormElement;
  if (form) {
    form.addEventListener("submit", validateScrubForm);
    form.privacy.addEventListener("input", hideErrorMessages);
    form.personalonly.addEventListener("input", hideErrorMessages);
    form.tos.addEventListener("input", hideErrorMessages);
    form.pii.addEventListener("input", hideErrorMessages);
    form.email.addEventListener("input", hideErrorMessages);
    form.denial_text.addEventListener("input", hideErrorMessages);
  } else {
    console.log("Missing form?!?");
  }
}

setupScrub();
