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
// its line. No blank lines: pdf.js gives no sign of one.
//
// Runs on one line are joined with a single space (none where one of them
// already brings its own), but not across a wide gap: a run that starts
// more than an em (the text's height) to the right of where the run before
// it ends is in another column, or past a tab stop, and the gap is kept as
// a tab. A letter often puts "Member ID: XYZ000000" in a right column on the
// street line's baseline, and joined with a space it read as part of the
// street. pdf.js marks a gap wider than 0.6 em with a " " run of its own,
// but the gap is measured here from where the runs sit (transform[4], and
// transform[4] plus width): one space in a monospaced letter is 0.6 em, so
// at pdf.js's own mark its words could come apart. A word space, and the
// space at a font change, stay a single space.

import type { TextItem, TextMarkedContent } from "pdfjs-dist/types/src/display/api";

// Only what this reads from an item, so a test can hand over plain objects.
export type PDFTextRun = Pick<TextItem, "str" | "hasEOL" | "transform" | "height" | "width">;

// Where a run sits on the page: its baseline, how tall its text is, and
// where it starts and ends across the page (null when that is not known).
interface Place {
  baseline: number;
  height: number;
  left: number | null;
  right: number | null;
}

// Null for a run that is not written along a level line (turned text): its
// height on the page says nothing about lines.
function placeOf(run: PDFTextRun): Place | null {
  const transform = run.transform;
  if (!Array.isArray(transform) || transform.length < 6) {
    return null;
  }
  const [across, tilt, , tall, x, baseline] = transform as number[];
  if (typeof baseline !== "number" || !isFinite(baseline) || Math.abs(tilt) > Math.abs(across) / 100) {
    return null;
  }
  const height = run.height > 0 ? run.height : Math.abs(tall) || 0;
  // Across the page only for text written left to right.
  const measurable = across > 0 && typeof x === "number" && isFinite(x);
  const left = measurable ? x : null;
  const right = measurable && typeof run.width === "number" && isFinite(run.width) ? x + run.width : null;
  return { baseline, height, left, right };
}

// Wider than this many ems (the taller text's height) is a gap between
// columns, not a space between words.
const WIDE_GAP_EMS = 1;

function isWideGap(before: Place | null, after: Place | null): boolean {
  if (before === null || after === null || before.right === null || after.left === null) {
    return false;
  }
  const em = Math.max(before.height, after.height);
  return em > 0 && after.left - before.right > em * WIDE_GAP_EMS;
}

export function textFromPDFItems(items: ReadonlyArray<PDFTextRun | TextMarkedContent>): string {
  const lines: string[] = [];
  let line = "";
  // The last run with text on this line whose place is known, and the run
  // with text right before this one (null when its place is not known).
  let lastPlace: Place | null = null;
  let runBefore: Place | null = null;
  const endLine = (): void => {
    const done = line.trim();
    if (done !== "") {
      lines.push(done);
    }
    line = "";
    lastPlace = null;
    runBefore = null;
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
      if (line !== "" && isWideGap(runBefore, place)) {
        line = line.replace(/\s+$/, "") + "\t" + str.replace(/^\s+/, "");
      } else {
        if (line !== "" && !/\s$/.test(line) && !/^\s/.test(str)) {
          line += " ";
        }
        line += str;
      }
      if (place !== null) {
        lastPlace = place;
      }
      runBefore = place;
    } else if (line !== "" && !/\s$/.test(line)) {
      // pdf.js's own space between two runs: one space. Whether the gap is
      // wide is read from where the next run starts, above.
      line += " ";
    }
    if (item.hasEOL) {
      endLine();
    }
  }
  endLine();
  return lines.join("\n");
}
