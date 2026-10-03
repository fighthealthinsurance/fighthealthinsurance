// Blanks left in a finished letter, like [Your Name], {{SCSID}} or XXX, and
// the notice that names them before the letter leaves the page.
//
// The patterns are letter_placeholders.json, the same file the fax form
// checks on the server (fighthealthinsurance/letter_placeholders.py), so a
// letter this page lets through to the fax is one the server accepts too.
// tests/sync/test_letter_placeholders.py runs both sides over the same
// letters and holds them to the same answers.

import spec from "../../letter_placeholders.json";

interface PatternEntry {
  pattern: string;
  flags?: string;
  label?: string;
}

const REFERENCE_DEFINITION: PatternEntry = spec.reference_links.definition;
const REFERENCE_LINK: PatternEntry = spec.reference_links.link;
const IGNORE: PatternEntry[] = spec.ignore;
const PLACEHOLDERS: PatternEntry[] = spec.placeholders;

export const PRINT_NOTICE_ID = "print-placeholder-notice";
export const FAX_NOTICE_ID = "fax-placeholder-notice";

// The fax form's answer to "send it as it is", by the name the server reads
// (FaxForm.send_with_placeholders). "Send anyway" adds it as a hidden field;
// the page the server sends back when it holds a letter offers it as a tick
// box, under the letter, with this id.
export const SEND_ANYWAY_FIELD = "send_with_placeholders";
export const SEND_ANYWAY_INPUT_ID = "fax-send-anyway";
export const SEND_ANYWAY_BOX_ID = "id_send_with_placeholders";

// The blanks "Send anyway" was pressed for, as the notice listed them. A
// letter whose blanks are all on this list is faxed as it is; one with a
// blank that is not on it waits, and the notice lists its blanks again.
const blanksSentAnyway: string[] = [];

function blankOut(match: string): string {
  return " ".repeat(match.length);
}

function everyMatchOf(entry: PatternEntry): RegExp {
  return new RegExp(entry.pattern, (entry.flags || "") + "g");
}

// A reference link's id as it is matched: capitals and runs of spaces don't
// count. Only A to Z fold, the same as on the server.
function referenceId(text: string): string {
  return text
    .replace(/[A-Z]/g, (capital: string) => capital.toLowerCase())
    .replace(/[ \t]+/g, " ")
    .replace(/^ | $/g, "");
}

// Blank out each reference link the letter defines, and each definition:
// [Coverage Policy][1] with [1]: https://example.com/policy on a line of its
// own is a link, not a blank. A reference link to an id the letter never
// defines is left as it is, for the placeholder patterns.
function blankOutReferenceLinks(text: string): string {
  const defined: string[] = [];
  const rest = text.replace(everyMatchOf(REFERENCE_DEFINITION), (whole: string) => {
    // The match can start with the line break before the definition, which
    // stays, so the lines stay apart.
    const start = whole.indexOf("[");
    defined.push(referenceId(whole.slice(start + 1, whole.indexOf("]", start))));
    return whole.slice(0, start) + blankOut(whole.slice(start));
  });
  return rest.replace(everyMatchOf(REFERENCE_LINK), (whole: string) => {
    const inner = whole.slice(1, -1);
    const split = inner.indexOf("][");
    const foundId = referenceId(inner.slice(split + 2) || inner.slice(0, split));
    return foundId && defined.indexOf(foundId) >= 0 ? blankOut(whole) : whole;
  });
}

interface PlaceholderSpot {
  // Where the blank starts in the letter, and how long it is there.
  at: number;
  length: number;
  // What the notice calls it: the blank itself, or its label (a line to
  // write on is listed as ___ however long it is).
  shown: string;
}

// Every blank in the letter, where it is, in the order it appears.
function findPlaceholderSpots(text: string): PlaceholderSpot[] {
  let rest = blankOutReferenceLinks(text || "");
  for (const entry of IGNORE) {
    rest = rest.replace(everyMatchOf(entry), blankOut);
  }
  // Each pattern claims what it matches, so a later one never reports part
  // of a blank an earlier one already found. The patterns have no capturing
  // groups, so the second argument is always the offset.
  const hits: PlaceholderSpot[] = [];
  for (const entry of PLACEHOLDERS) {
    rest = rest.replace(
      everyMatchOf(entry),
      (match: string, at: number) => {
        hits.push({ at: at, length: match.length, shown: entry.label || match });
        return blankOut(match);
      },
    );
  }
  hits.sort((a, b) => a.at - b.at);
  return hits;
}

// Each blank in the letter, once, in the order it first appears.
export function findUnfilledPlaceholders(text: string): string[] {
  const found: string[] = [];
  for (const hit of findPlaceholderSpots(text)) {
    if (found.indexOf(hit.shown) < 0) {
      found.push(hit.shown);
    }
  }
  return found;
}

export interface PlaceholderNotice {
  id: string;
  // The notice goes just above this, the button that was pressed.
  before: HTMLElement;
  letter: HTMLTextAreaElement | null;
  found: string[];
  heading: string;
  advice: string;
  // A way past the notice: paper can be filled in by hand, and a fax can go
  // as it is once the person has checked that what was found is not a blank.
  anyway?: { label: string; choose: () => void };
}

function removeById(id: string): void {
  const old = document.getElementById(id);
  if (old && old.parentNode) {
    old.parentNode.removeChild(old);
  }
}

export function removePlaceholderNotice(id: string): void {
  removeById(id);
}

function noticeButton(label: string, className: string): HTMLElement {
  const button = document.createElement("button");
  // Inside the fax form a plain button would submit it.
  button.setAttribute("type", "button");
  button.className = className;
  button.textContent = label;
  return button;
}

// Put the person's cursor on the first blank, selected, so typing replaces it.
// The letter is searched again as it is now, so the selection is the blank
// itself even after edits, and never the same characters inside something
// the check leaves alone (the XXX of a karyotype like 47,XXX).
function showFirstPlaceholder(letter: HTMLTextAreaElement | null): void {
  if (!letter) {
    return;
  }
  letter.focus();
  const first = findPlaceholderSpots(letter.value)[0];
  if (first) {
    letter.setSelectionRange(first.at, first.at + first.length);
  }
}

// An inline notice, never a dialog. It replaces any earlier notice with the
// same id and takes the focus, so a keyboard or screen reader user lands on
// it rather than past it.
export function showPlaceholderNotice(notice: PlaceholderNotice): HTMLElement {
  removePlaceholderNotice(notice.id);

  const box = document.createElement("div");
  box.id = notice.id;
  box.className = "fhi-notice fhi-notice-warning fhi-stack";
  box.setAttribute("tabindex", "-1");
  box.setAttribute("aria-labelledby", notice.id + "-heading");

  const heading = document.createElement("p");
  heading.id = notice.id + "-heading";
  const strong = document.createElement("strong");
  strong.textContent = notice.heading;
  heading.appendChild(strong);
  box.appendChild(heading);

  const leadIn = document.createElement("p");
  leadIn.textContent = "These look like spots meant for your own details:";
  box.appendChild(leadIn);

  // The blanks are the letter's own text, so they go in as text, never as
  // markup.
  const list = document.createElement("ul");
  for (const placeholder of notice.found) {
    const item = document.createElement("li");
    item.textContent = placeholder;
    list.appendChild(item);
  }
  box.appendChild(list);

  const advice = document.createElement("p");
  advice.textContent = notice.advice;
  box.appendChild(advice);

  const actions = document.createElement("div");
  actions.className = "fhi-cluster";
  const showMe = noticeButton("Show me in the letter", "fhi-button fhi-button-secondary");
  const letter = notice.letter;
  showMe.addEventListener("click", () => showFirstPlaceholder(letter));
  actions.appendChild(showMe);
  const anyway = notice.anyway;
  if (anyway) {
    const goAhead = noticeButton(anyway.label, "fhi-button fhi-button-neutral");
    goAhead.addEventListener("click", () => anyway.choose());
    actions.appendChild(goAhead);
  }
  box.appendChild(actions);

  const parent = notice.before.parentNode;
  if (parent) {
    parent.insertBefore(box, notice.before);
  } else {
    document.body.appendChild(box);
  }
  box.focus();
  return box;
}

// Print straight away when the letter is complete. Otherwise name the
// blanks, with a way to find them and a way to print anyway: a paper letter
// can be filled in by hand.
export function printUnlessPlaceholders(
  button: HTMLElement,
  letter: HTMLTextAreaElement | null,
  print: () => void,
): void {
  const found = findUnfilledPlaceholders(letter ? letter.value : "");
  if (found.length === 0) {
    removePlaceholderNotice(PRINT_NOTICE_ID);
    print();
    return;
  }
  showPlaceholderNotice({
    id: PRINT_NOTICE_ID,
    before: button,
    letter: letter,
    found: found,
    heading: "Your letter still has blanks to fill in",
    advice:
      "Replace each one with your details, or delete it if it doesn't apply." +
      " If you'd rather write them in by hand, you can print the letter as it is.",
    anyway: {
      label: "Print anyway",
      choose: () => {
        // The notice goes, and the focus goes back to the Print button
        // rather than nowhere.
        removePlaceholderNotice(PRINT_NOTICE_ID);
        button.focus();
        print();
      },
    },
  });
}

// The hidden field that tells the server to fax the letter as it is: on the
// form for a submission "Send anyway" started, and off it for every other.
function markSentAnyway(form: HTMLFormElement, sentAnyway: boolean): void {
  removeById(SEND_ANYWAY_INPUT_ID);
  if (!sentAnyway) {
    return;
  }
  const field = document.createElement("input");
  field.id = SEND_ANYWAY_INPUT_ID;
  field.setAttribute("type", "hidden");
  field.setAttribute("name", SEND_ANYWAY_FIELD);
  field.setAttribute("value", "1");
  form.appendChild(field);
}

// True when the fax has to wait: the letter still has blanks, now named just
// above the fax button. The insurance company would get them exactly as
// written, and the server holds the same letter. Some of what the check
// finds is not a blank (an acronym in brackets like [ERISA], a name typed
// inside the brackets), so the notice offers "Send anyway", which means
// "send these": the blanks it listed, and no others. It presses the fax
// button again, and from then on a letter whose blanks were all listed goes
// without the notice and carries the answer the server reads. A letter
// edited since, with a blank in it that was not listed, waits, and the
// notice comes back listing every blank it has.
// A ticked "Send it as it is" box, on the page the server sends back, is the
// same answer and lets every submission through while it stays ticked.
export function faxMustWaitForPlaceholders(
  form: HTMLFormElement,
  button: HTMLElement,
  letter: HTMLTextAreaElement | null,
): boolean {
  const found = findUnfilledPlaceholders(letter ? letter.value : "");
  const box = document.getElementById(SEND_ANYWAY_BOX_ID) as HTMLInputElement | null;
  const ticked = box !== null && box.checked;
  const allSentAnyway = found.every((blank) => blanksSentAnyway.indexOf(blank) >= 0);
  markSentAnyway(form, found.length > 0 && allSentAnyway && !ticked);
  if (found.length === 0 || allSentAnyway || ticked) {
    removePlaceholderNotice(FAX_NOTICE_ID);
    return false;
  }
  showPlaceholderNotice({
    id: FAX_NOTICE_ID,
    before: button,
    letter: letter,
    found: found,
    heading: "Fill in these blanks before we fax your letter",
    advice:
      "Your insurance company would get them exactly as written." +
      " Replace each one with your details, or delete it if it doesn't apply," +
      " then send the fax again. If you've checked and these are not blanks," +
      " you can send it as it is.",
    anyway: {
      label: "Send anyway",
      choose: () => {
        // The notice goes, and the focus goes to the fax button rather than
        // nowhere, while the fax is sent or if the browser stops it.
        removePlaceholderNotice(FAX_NOTICE_ID);
        button.focus();
        // The blanks this notice listed, not the letter as it is now: one
        // typed in since the notice showed still waits for a notice of its
        // own.
        for (const blank of found) {
          if (blanksSentAnyway.indexOf(blank) < 0) {
            blanksSentAnyway.push(blank);
          }
        }
        button.click();
      },
    },
  });
  return true;
}
