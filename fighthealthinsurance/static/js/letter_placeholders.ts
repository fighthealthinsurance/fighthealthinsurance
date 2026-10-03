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

// The blanks the person said to fax as they are, by the name the server
// reads (FaxForm.approved_placeholders): a JSON list, each blank exactly as
// the letter has it. The server faxes a letter with blanks only when every
// one is on a list posted under this name. "Send anyway" posts one in a
// hidden field; the page the server sends back when it holds a letter has a
// "Send it as it is" tick box under the letter, with this id, whose value is
// the list that page names.
export const SEND_ANYWAY_FIELD = "approved_placeholders";
export const SEND_ANYWAY_INPUT_ID = "fax-send-anyway";
export const SEND_ANYWAY_BOX_ID = "id_approved_placeholders";

// The blanks "Send anyway" was pressed for, as the letter had them when the
// notice listed them. A letter whose blanks are all approved, here or by the
// ticked box, is faxed as it is; one with a blank that is not waits, and the
// notice lists its blanks again, the new ones first.
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
  // The blank exactly as the letter has it: what a person says yes to.
  written: string;
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
        hits.push({
          at: at,
          length: match.length,
          written: match,
          shown: entry.label || match,
        });
        return blankOut(match);
      },
    );
  }
  hits.sort((a, b) => a.at - b.at);
  return hits;
}

function onceEach(values: string[]): string[] {
  const found: string[] = [];
  for (const value of values) {
    if (found.indexOf(value) < 0) {
      found.push(value);
    }
  }
  return found;
}

// Each blank in the letter, once, in the order it first appears.
export function findUnfilledPlaceholders(text: string): string[] {
  return onceEach(findPlaceholderSpots(text).map((hit) => hit.shown));
}

// Each blank exactly as the letter has it, once, in the order it first
// appears: what "Send anyway" says yes to. The same list as
// findUnfilledPlaceholders, but for a line to write on, which is itself here
// rather than ___, so a line of another length is another blank. The server
// makes the same list (find_placeholders_as_written).
export function findPlaceholdersAsWritten(text: string): string[] {
  return onceEach(findPlaceholderSpots(text).map((hit) => hit.written));
}

export interface PlaceholderNotice {
  id: string;
  // The notice goes just above this, the button that was pressed.
  before: HTMLElement;
  letter: HTMLTextAreaElement | null;
  found: string[];
  // Blanks from found that are new since the person said to send the letter
  // as it is. They are listed first, each marked "New".
  fresh?: string[];
  // Whether the person has said yes to a blank, as the letter has it. "Show
  // me in the letter" goes to the first blank they have not.
  isApproved?: (written: string) => boolean;
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

// Put the person's cursor on the first blank they have not said yes to,
// selected, so typing replaces it; with none, on the first blank. The letter
// is searched again as it is now, so the selection is the blank itself even
// after edits, and never the same characters inside something the check
// leaves alone (the XXX of a karyotype like 47,XXX).
function showFirstPlaceholder(
  letter: HTMLTextAreaElement | null,
  isApproved?: (written: string) => boolean,
): void {
  if (!letter) {
    return;
  }
  letter.focus();
  const spots = findPlaceholderSpots(letter.value);
  let first = spots[0];
  for (const spot of spots) {
    if (!isApproved || !isApproved(spot.written)) {
      first = spot;
      break;
    }
  }
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

  const fresh = notice.fresh || [];
  const leadIn = document.createElement("p");
  leadIn.textContent =
    fresh.length > 0
      ? "These look like spots meant for your own details. The ones marked" +
        " new weren't there when you said to send it as it is:"
      : "These look like spots meant for your own details:";
  box.appendChild(leadIn);

  // The new blanks first, each marked, then the rest. The blanks are the
  // letter's own text, so they go in as text, never as markup.
  const list = document.createElement("ul");
  const inOrder = fresh.concat(
    notice.found.filter((placeholder) => fresh.indexOf(placeholder) < 0),
  );
  for (const placeholder of inOrder) {
    const item = document.createElement("li");
    if (fresh.indexOf(placeholder) >= 0) {
      const mark = document.createElement("strong");
      mark.textContent = "New:";
      item.appendChild(mark);
      item.appendChild(document.createTextNode(" " + placeholder));
    } else {
      item.textContent = placeholder;
    }
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
  const isApproved = notice.isApproved;
  showMe.addEventListener("click", () => showFirstPlaceholder(letter, isApproved));
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

// The blanks the ticked "Send it as it is" box says yes to, from its value;
// none while it is unticked, or not on the page.
function blanksTheBoxApproves(): string[] {
  const box = document.getElementById(SEND_ANYWAY_BOX_ID) as HTMLInputElement | null;
  if (!box || !box.checked) {
    return [];
  }
  try {
    const listed: unknown = JSON.parse(box.getAttribute("value") || "");
    if (Array.isArray(listed)) {
      return listed.filter((blank): blank is string => typeof blank === "string");
    }
  } catch (notAList) {
    // A box with no list says yes to nothing.
  }
  return [];
}

// Whether the person has said yes to this blank, as the letter has it: with
// "Send anyway", or with the ticked box.
function isApprovedBlank(written: string): boolean {
  return (
    blanksSentAnyway.indexOf(written) >= 0 || blanksTheBoxApproves().indexOf(written) >= 0
  );
}

// The hidden field that tells the server which blanks to fax as they are:
// on the form, holding them, for a submission that goes with blanks the
// person said yes to, and off it for every other.
function markSentAnyway(form: HTMLFormElement, approved: string[]): void {
  removeById(SEND_ANYWAY_INPUT_ID);
  if (approved.length === 0) {
    return;
  }
  const field = document.createElement("input");
  field.id = SEND_ANYWAY_INPUT_ID;
  field.setAttribute("type", "hidden");
  field.setAttribute("name", SEND_ANYWAY_FIELD);
  field.setAttribute("value", JSON.stringify(approved));
  form.appendChild(field);
}

// True when the fax has to wait: the letter still has blanks, now named just
// above the fax button. The insurance company would get them exactly as
// written, and the server holds the same letter. Some of what the check
// finds is not a blank (an acronym in brackets like [ERISA], a name typed
// inside the brackets), so the notice offers "Send anyway", which means
// "send these": the blanks it listed, and no others. It presses the fax
// button again, and from then on a letter whose blanks were all listed goes
// without the notice, posting them as approved. A letter edited since, with
// a blank in it that was not listed, waits, and the notice comes back
// listing every blank it has, the new ones first and marked.
// A ticked "Send it as it is" box, on the page the server sends back, says
// yes to the blanks that page listed, and to no others.
export function faxMustWaitForPlaceholders(
  form: HTMLFormElement,
  button: HTMLElement,
  letter: HTMLTextAreaElement | null,
): boolean {
  const spots = findPlaceholderSpots(letter ? letter.value : "");
  const written = onceEach(spots.map((spot) => spot.written));
  const waiting = written.filter((blank) => !isApprovedBlank(blank));
  if (waiting.length === 0) {
    markSentAnyway(form, written);
    removePlaceholderNotice(FAX_NOTICE_ID);
    return false;
  }
  markSentAnyway(form, []);
  // Marked new only when the person has said yes to some of the others:
  // on a first notice every blank is one they have not seen.
  const fresh =
    waiting.length < written.length
      ? onceEach(
          spots
            .filter((spot) => waiting.indexOf(spot.written) >= 0)
            .map((spot) => spot.shown),
        )
      : [];
  showPlaceholderNotice({
    id: FAX_NOTICE_ID,
    before: button,
    letter: letter,
    found: onceEach(spots.map((spot) => spot.shown)),
    fresh: fresh,
    isApproved: isApprovedBlank,
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
        for (const blank of written) {
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
