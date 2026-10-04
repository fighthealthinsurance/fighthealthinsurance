// The review page for one regulator letter (escalation_packet_review.html).
//
// The letter is drafted with blanks for the person's details ({{FIRST_NAME}},
// {{SCSID}} and so on) for them to fill in here, so Print checks for any that
// are left before it opens the print window, the same way the appeal page
// does. A paper letter can be filled in by hand, so the notice offers to
// print anyway.

import { printUnlessPlaceholders } from "./letter_placeholders";

const ESCAPES: { [c: string]: string } = { "<": "&lt;", ">": "&gt;", "&": "&amp;" };

// A letter is text: it is escaped before it is written into the window.
function printRegulatorLetter(text: string): void {
  const w = window.open("", "_blank");
  if (!w) {
    return;
  }
  w.document.write(
    '<html><head><title>Regulator letter</title></head><body><pre style="white-space:pre-wrap; font-family: Georgia, serif;">' +
      text.replace(/[<>&]/g, (c) => ESCAPES[c]) +
      "</pre></body></html>",
  );
  w.document.close();
  setTimeout(() => w.print(), 500);
}

function setupEscalationPacketReview(): void {
  const printButton = document.getElementById("print_appeal");
  const letter = document.getElementById(
    "id_completed_appeal_text",
  ) as HTMLTextAreaElement | null;
  if (!printButton || !letter) {
    return;
  }
  printButton.addEventListener("click", () => {
    printUnlessPlaceholders(printButton, letter, () => printRegulatorLetter(letter.value));
  });
}

setupEscalationPacketReview();
