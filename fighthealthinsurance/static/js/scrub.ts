import { storeLocal, storeTextareaLocal, getLocalStorageItemWithTTL, setLocalStorageItemWithTTL, isPersistenceEnabled, setPersistenceEnabled } from "./shared";

import { recognize } from "./scrub_ocr";

import { clean } from "./scrub_scrub";

import {
  addText,
  beginOcr,
  clearOcrFailure,
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

/**
 * Only the user's explicit "save data" request, NOT merely being on a phone.
 *
 * This used to switch the better OCR engine off for every cellular or 2g
 * connection, which had the quality argument exactly backwards: a photo taken
 * on a phone is the hardest input we get, and Qwen is the engine best at
 * reading it. So the one case we turn it off for is someone who has actually
 * asked their browser to conserve data -- Qwen downloads a model, and
 * overriding that request would be rude. Everything else is decided by
 * whether the DEVICE can run it.
 */
function userAskedToSaveData(): boolean {
  const connection = (navigator as Navigator & { connection?: NetworkInformation }).connection;
  return connection?.saveData === true;
}

async function initAdvancedOCRCheckbox(): Promise<void> {
  const checkbox = document.getElementById("advanced_ocr_enabled") as HTMLInputElement | null;
  if (!checkbox) return;

  if (userAskedToSaveData()) {
    checkbox.checked = false;
    return;
  }

  // Capability, not connection: without WebGPU it cannot run at all, and on
  // a very small device it will fight the page for memory.
  const memoryGb = (navigator as Navigator & { deviceMemory?: number }).deviceMemory;
  if (typeof memoryGb === "number" && memoryGb <= 2) {
    checkbox.checked = false;
    return;
  }

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
  if (filesArray.length === 0) {
    // An EMPTY FileList is truthy, so the null check above does not catch it,
    // and browsers fire `change` with one when a selection is cleared. The
    // loop then does nothing, failures stays 0 and ocrChars stays 0, so the
    // verdict below reported "we couldn't read your file" about a file the
    // user had just removed. Harmless before this branch existed, because no
    // verdict was reported at all.
    //
    // Deliberately does NOT take a selection number: clearing the input is
    // not a new read, and superseding an in-flight batch would throw away
    // text that is still arriving from a file the user did choose.
    clearOcrFailure();
    return;
  }

  const selection = ++latestOcrSelection;

  // Mark OCR as in flight for the whole batch so the submit gate can tell the
  // user we are still reading their file rather than letting them submit an
  // empty denial_text. endOcr() must run even when recognize() throws.
  //
  // A fresh selection replaces the previous verdict: the last attempt having
  // failed says nothing about this one.
  clearOcrFailure();
  let failures = 0;
  let ocrChars = 0;

  // Guard the CALLBACK, not just the verdict. recognize() invokes this after
  // async OCR work, so a superseded batch could still append its text into
  // denial_text long after the user picked different files -- they would get
  // the old document's text back with no way to know why.
  //
  // Counting here also keeps the verdict honest about who produced what.
  // Measuring the textarea before and after instead would count the USER's
  // typing: someone typing while a doomed batch runs made it look like OCR
  // had produced something, which reported partial success and re-posted a
  // message their typing had just cleared.
  const addTextForThisSelection = (text: string): void => {
    if (selection !== latestOcrSelection) {
      return;
    }
    ocrChars += text.trim().length;
    addText(text);
  };

  beginOcr();
  try {
    for (const file of filesArray) {
      // Catch PER FILE, not around the loop. The uploader is multiple="true"
      // and people attach a denial one page per image; with a single catch
      // outside, one unreadable page abandoned every page after it and the
      // user was never told which -- or that anything had gone wrong at all.
      try {
        await recognize(file, addTextForThisSelection);
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
  // enough. Treat "produced no text" as a failure too.
  const producedText = ocrChars > 0;
  if (failures > 0 && producedText) {
    // Some pages read and some did not. Saying "we couldn't read your file"
    // here would be plainly false with their text sitting right below it.
    notePartialOcrFailure();
  } else if (failures > 0 || !producedText) {
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
