import { storeLocal } from "./shared";

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
  const filesArray = Array.from(files);

  for (const file of filesArray) {
    await recognize(file, addText);
  }
};

function setupScrub(): void {
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
        node.value = window.localStorage.getItem(node.id);
      }
    }
  }
  nodes.forEach(handleStorage);
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
