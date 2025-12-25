import { recognize } from "./scrub_ocr";

/**
 * OCR functionality for the Explain My Denial page.
 * This module handles file uploads and performs client-side OCR,
 * similar to the appeal flow's scrub.ts, but simplified for the explain denial use case.
 */

/**
 * Appends recognized text to the denial_text textarea.
 */
function addText(text: string): void {
  const textarea = document.getElementById("denial_text") as HTMLTextAreaElement;
  if (textarea) {
    // Append new text with a separator if textarea already has content
    if (textarea.value.trim().length > 0) {
      textarea.value += "\n\n" + text;
    } else {
      textarea.value = text;
    }
    // Trigger input event to ensure any form validation is updated
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
  }
}

/**
 * Handles file upload and OCR recognition.
 */
const recognizeEvent = async function (evt: Event) {
  const input = evt.target as HTMLInputElement;
  const files = input.files;

  if (!files) {
    return; // Exit early if files is null
  }

  const filesArray = Array.from(files);

  // Show a loading indicator or message
  const textarea = document.getElementById("denial_text") as HTMLTextAreaElement;
  if (textarea && filesArray.length > 0) {
    textarea.placeholder = "Processing your files... This may take a moment.";
  }

  for (const file of filesArray) {
    await recognize(file, addText);
  }

  // Reset placeholder
  if (textarea) {
    textarea.placeholder = "Paste your denial letter here, or upload files above to automatically extract text...";
  }
};

/**
 * Initialize the explain denial page OCR functionality.
 */
function setupExplainDenialOCR(): void {
  const uploader = document.getElementById("uploader");
  if (uploader != null) {
    uploader.addEventListener("change", recognizeEvent);
  }
}

// Initialize when DOM is ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', setupExplainDenialOCR);
} else {
  // DOMContentLoaded already fired
  setupExplainDenialOCR();
}
