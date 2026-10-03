// The text of one PDF page, from pdf.js's text items, with the page's line
// breaks kept.
//
// pdf.js hands a page's text layer over as runs of text ("items"), each with
// its position, and marks a run that ends a line (hasEOL). Joining every run
// with a space, as this used to, put the whole page on one line: the
// addressee's block ran into the date and the salutation, so About you
// could not be filled from a text PDF (letter_details.ts), and the letter
// box read as one long line. So a line ends after a run marked hasEOL, and
// also where the next run sits on another line (its baseline, transform[5],
// moves by more than half the text's height), since pdf.js does not mark
// every break. A superscript or subscript moves less than that and stays on
// its line. Runs on one line are joined with a single space, unless one of
// them already brings its own. No blank lines: pdf.js gives no sign of one.

import type { TextItem, TextMarkedContent } from "pdfjs-dist/types/src/display/api";

// Only what this reads from an item, so a test can hand over plain objects.
export type PDFTextRun = Pick<TextItem, "str" | "hasEOL" | "transform" | "height">;

// Where a run sits on the page, top to bottom, and how tall its text is.
// Null for a run that is not written left to right along a level line
// (turned text): its height on the page says nothing about lines.
function placeOf(run: PDFTextRun): { baseline: number; height: number } | null {
  const transform = run.transform;
  if (!Array.isArray(transform) || transform.length < 6) {
    return null;
  }
  const [across, tilt, , tall, , baseline] = transform as number[];
  if (typeof baseline !== "number" || !isFinite(baseline) || Math.abs(tilt) > Math.abs(across) / 100) {
    return null;
  }
  const height = run.height > 0 ? run.height : Math.abs(tall) || 0;
  return { baseline, height };
}

export function textFromPDFItems(items: ReadonlyArray<PDFTextRun | TextMarkedContent>): string {
  const lines: string[] = [];
  let line = "";
  // The last run with text on this line.
  let lastPlace: { baseline: number; height: number } | null = null;
  const endLine = (): void => {
    const done = line.trim();
    if (done !== "") {
      lines.push(done);
    }
    line = "";
    lastPlace = null;
  };
  for (const item of items) {
    // Marked content (a tag around some runs) carries no text.
    if (!("str" in item)) {
      continue;
    }
    const str = item.str;
    if (str.trim() !== "") {
      const place = placeOf(item);
      if (lastPlace !== null && place !== null) {
        const tallest = Math.max(lastPlace.height, place.height, 2);
        if (Math.abs(place.baseline - lastPlace.baseline) > tallest / 2) {
          endLine();
        }
      }
      if (line !== "" && !/\s$/.test(line) && !/^\s/.test(str)) {
        line += " ";
      }
      line += str;
      if (place !== null) {
        lastPlace = place;
      }
    } else if (line !== "" && !/\s$/.test(line)) {
      // pdf.js's own space between two runs: one, however wide the gap.
      line += " ";
    }
    if (item.hasEOL) {
      endLine();
    }
  }
  endLine();
  return lines.join("\n");
}
