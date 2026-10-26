/**
 * OCR functionality for the Explain My Denial page.
 *
 * Reuses existing OCR infrastructure from scrub_ocr.ts to allow
 * users to upload denial letter images/PDFs and extract text.
 */

import { recognize } from "./scrub_ocr";

const MAX_UPLOAD_SIZE_BYTES = 20 * 1024 * 1024;

/**
 * Add recognized text to the denial_text textarea.
 * Appends text with a newline separator if textarea already has content.
 */
function addText(text: string): void {
  const textarea = document.getElementById(
    "denial_text"
  ) as HTMLTextAreaElement | null;
  if (!textarea) {
    console.error("denial_text textarea not found");
    return;
  }

  // Append with separator if there's existing content
  if (textarea.value.trim().length > 0) {
    textarea.value += "\n\n---\n\n";
  }
  textarea.value += text;
}

/**
 * Handle file upload events from the uploader element.
 * Processes each file through OCR and adds text to the textarea.
 */
async function recognizeEvent(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement;
  const files = input.files;

  if (!files || files.length === 0) {
    return;
  }

  const fileList = Array.from(files);
  const oversizedFiles = fileList.filter(
    (file) => file.size > MAX_UPLOAD_SIZE_BYTES
  );
  if (oversizedFiles.length > 0) {
    const tooLargeNames = oversizedFiles.map((file) => file.name).join(", ");
    alert(
      `These files exceed the 20MB upload limit and were not processed: ${tooLargeNames}`
    );
  }

  const filesToProcess = fileList.filter(
    (file) => file.size <= MAX_UPLOAD_SIZE_BYTES
  );
  if (filesToProcess.length === 0) {
    input.value = "";
    return;
  }

  // Say the file is being read, just above the box it is read into.
  const status = document.getElementById("ocr_status");
  if (status) {
    status.textContent = "We're reading your file. The text will appear in the box below.";
    status.hidden = false;
  }

  try {
    for (let i = 0; i < filesToProcess.length; i++) {
      const file = filesToProcess[i];
      // Don't log file.name: user filenames can embed patient names.
      console.log(`Processing file ${i + 1}/${filesToProcess.length}`);
      await recognize(file, addText);
    }
  } catch (error) {
    console.error("Error processing file(s):", error);
    alert(
      "Error processing file. Please try again or paste text manually."
    );
  } finally {
    if (status) {
      status.hidden = true;
      status.textContent = "";
    }
    // Clear the input so the same file can be re-uploaded if needed
    input.value = "";
  }
}

/**
 * Set up OCR event listeners for the explain denial page. A file dropped on
 * the letter box comes here too: file_drop.js hands it to the uploader and
 * fires its change event.
 */
function setupExplainDenialOCR(): void {
  const uploader = document.getElementById("uploader");
  if (uploader) {
    uploader.addEventListener("change", recognizeEvent);
    console.log("Explain denial OCR initialized");
  }
}

// Initialize when DOM is ready
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", setupExplainDenialOCR);
} else {
  setupExplainDenialOCR();
}
