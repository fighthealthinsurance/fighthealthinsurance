/**
 * OCR functionality for the Explain My Denial page.
 *
 * Reuses existing OCR infrastructure from scrub_ocr.ts to allow
 * users to upload denial letter images/PDFs and extract text.
 */

import { recognize } from "./scrub_ocr";

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

  // Show processing indicator
  const uploadLabel = document.querySelector(
    'label[for="uploader"]'
  ) as HTMLLabelElement | null;
  const originalLabelText = uploadLabel?.textContent || "";
  if (uploadLabel) {
    uploadLabel.textContent = "Processing...";
  }

  try {
    for (let i = 0; i < files.length; i++) {
      const file = files[i];
      console.log(`Processing file ${i + 1}/${files.length}: ${file.name}`);
      await recognize(file, addText);
    }
  } catch (error) {
    console.error("Error processing file(s):", error);
    alert(
      "Error processing file. Please try again or paste text manually."
    );
  } finally {
    // Restore label text
    if (uploadLabel) {
      uploadLabel.textContent = originalLabelText;
    }
    // Clear the input so the same file can be re-uploaded if needed
    input.value = "";
  }
}

/**
 * Set up OCR event listeners for the explain denial page.
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
