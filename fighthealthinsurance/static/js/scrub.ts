import { storeLocal, storeTextareaLocal, getLocalStorageItemWithTTL, setLocalStorageItemWithTTL, isPersistenceEnabled, setPersistenceEnabled } from "./shared";

import { recognize } from "./scrub_ocr";

import { clean } from "./scrub_scrub";

import {
  addText,
  hideErrorMessages,
  validateScrubForm,
} from "./scrub_client_side_form";

const recognizeEvent = async function (evt: Event) {
  const input = evt.target as HTMLInputElement;
  const files = input.files;

  if (!files) {
    return; // Exit early if files is null
  }

  const filesArray = Array.from(files);

  for (const file of filesArray) {
    await recognize(file, addText);
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

  // Restore previous local values
  var input = document.getElementsByTagName("input");
  console.log(input);
  var nodes: NodeListOf<HTMLInputElement> = document.querySelectorAll("input");
  console.log("Nodes:");
  console.log(nodes);
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
