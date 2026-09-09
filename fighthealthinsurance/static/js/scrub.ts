import { storeLocal, storeTextareaLocal, getLocalStorageItemWithTTL, setLocalStorageItemWithTTL, isPersistenceEnabled, setPersistenceEnabled } from "./shared";

import { type OnDeviceRead, containsNormalised, isAdvancedOCREnabled, recognize } from "./scrub_ocr";

import { clean } from "./scrub_scrub";

import {
  addText,
  beginOcr,
  clearOcrFailure,
  endOcr,
  hideErrorMessages,
  notePartialOcrFailure,
  noteOcrFailure,
  validateScrubForm,
} from "./scrub_client_side_form";

import {
  detectWebGPUAvailability,
  giveUpOnDeviceModel,
  onDeviceGenerationSettled,
  onDeviceLoadMayStillBeRunning,
  onDeviceModelLoadFailed,
} from "./qwen_webgpu_ocr";

// Network Information API (not yet in standard TypeScript lib types).
interface NetworkInformation {
  type?: string;
  effectiveType?: string;
  saveData?: boolean;
}

/**
 * Only the user's explicit "save data" request, NOT merely being on a phone.
 *
 * This used to switch the better OCR engine off for every cellular or 2g
 * connection, which had the quality argument exactly backwards: a photo taken
 * on a phone is the hardest input we get, and Qwen is the engine best at
 * reading it. So the one case we turn it off for is someone who has actually
 * asked their browser to conserve data -- Qwen downloads a model, and
 * overriding that request would be rude. Everything else is decided by
 * whether the DEVICE can run it.
 */
function userAskedToSaveData(): boolean {
  const connection = (navigator as Navigator & { connection?: NetworkInformation }).connection;
  return connection?.saveData === true;
}

/**
 * Screens the advanced-OCR checkbox down, never up.
 *
 * The box now ships unchecked, so today every branch here is a no-op and the
 * screening below is belt-and-braces. It is kept rather than deleted because
 * the moment the engine works and the default goes back on, these are exactly
 * the devices that must not get it, and rebuilding the screening from scratch
 * is how it comes back wrong.
 */
async function initAdvancedOCRCheckbox(): Promise<void> {
  const checkbox = document.getElementById("advanced_ocr_enabled") as HTMLInputElement | null;
  if (!checkbox) return;

  // Capability first, on every path: a box that cannot work here must not
  // be turnable on, whatever the defaults below decide (review: the
  // data-saver and small-device screens returned before this ran).
  const webGpu = await detectWebGPUAvailability();
  if (!webGpu.available) {
    checkbox.checked = false;
    checkbox.disabled = true;
    checkbox.title = "Not available in this browser";
    return;
  }

  if (userAskedToSaveData()) {
    checkbox.checked = false;
    return;
  }

  // Connection is not the question, memory is: on a very small device the
  // model will fight the page for it.
  const memoryGb = (navigator as Navigator & { deviceMemory?: number }).deviceMemory;
  if (typeof memoryGb === "number" && memoryGb <= 2) {
    checkbox.checked = false;
  }
}

// Which selection is current. Reading a batch is slow enough that a user can
// pick again while one is still running, and only the newest pick describes
// what is on screen: without this, a slow FAILING batch could finish after a
// newer successful one and re-post "we couldn't read your file" over text
// that had just arrived.
let latestOcrSelection = 0;

const recognizeEvent = async function (evt: Event) {
  const input = evt.target as HTMLInputElement;
  const files = input.files;

  if (!files) {
    return; // Exit early if files is null
  }

  const filesArray = Array.from(files);
  if (filesArray.length === 0) {
    // An EMPTY FileList is truthy, so the null check above does not catch it,
    // and browsers fire `change` with one when a selection is cleared. The
    // loop then does nothing, failures stays 0 and ocrChars stays 0, so the
    // verdict below reported "we couldn't read your file" about a file the
    // user had just removed. Harmless before this branch existed, because no
    // verdict was reported at all.
    //
    // Deliberately does NOT take a selection number: clearing the input is
    // not a new read, and superseding an in-flight batch would throw away
    // text that is still arriving from a file the user did choose.
    clearOcrFailure();
    return;
  }

  const selection = ++latestOcrSelection;

  // Mark OCR as in flight for the whole batch so the submit gate can tell the
  // user we are still reading their file rather than letting them submit an
  // empty denial_text. endOcr() must run even when recognize() throws.
  //
  // A fresh selection replaces the previous verdict: the last attempt having
  // failed says nothing about this one.
  clearOcrFailure();
  let failures = 0;
  let ocrChars = 0;

  // Guard the CALLBACK, not just the verdict. recognize() invokes this after
  // async OCR work, so a superseded batch could still append its text into
  // denial_text long after the user picked different files -- they would get
  // the old document's text back with no way to know why.
  //
  // Counting here also keeps the verdict honest about who produced what.
  // Measuring the textarea before and after instead would count the USER's
  // typing: someone typing while a doomed batch runs made it look like OCR
  // had produced something, which reported partial success and re-posted a
  // message their typing had just cleared.
  const textarea = document.getElementById("denial_text") as HTMLTextAreaElement;
  // Whatever reached the box without an input event since the last look
  // (a Remove PII pass, a script) is absorbed before the baseline is taken,
  // so the first keystroke during this pass is diffed against the box as it
  // really is (page check: a stale baseline marked the chunk dirty).
  syncTracker(textarea.value);
  const editsAtStart = userEditsSeen;
  // A new selection supersedes every earlier one: its controls are gone and
  // its text is no longer followed. This one's text is followed from where
  // the box ends now, and every page is recorded as it goes out, in order,
  // so the model's later reading knows where each page sits without ever
  // searching for it.
  clearOnDeviceStatus();
  trackedChunks.clear();
  const chunk = trackChunk(textarea.value.length);
  const pieces: EmittedPiece[] = [];
  const addTextForThisSelection = (text: string, read?: OnDeviceRead): void => {
    if (selection !== latestOcrSelection) {
      return;
    }
    ocrChars += text.trim().length;
    // Anything written to the box since the last look is diffed in first,
    // so the append lands on positions that are current.
    syncTracker(textarea.value);
    const wasLength = textarea.value.length;
    addText(text);
    noteProgrammaticAppend(chunk, wasLength, textarea.value);
    pieces.push({ text, read });
  };

  beginOcr(selection);
  try {
    for (const file of filesArray) {
      // Catch PER FILE, not around the loop. The uploader is multiple="true"
      // and people attach a denial one page per image; with a single catch
      // outside, one unreadable page abandoned every page after it and the
      // user was never told which -- or that anything had gone wrong at all.
      try {
        await recognize(file, addTextForThisSelection);
      } catch (error) {
        failures += 1;
        // The filename can carry the patient's name; keep it out of logs.
        console.error("OCR failed for an uploaded file:", error);
      }
    }
  } finally {
    endOcr(selection);
  }

  // A superseded batch says nothing about what the user is looking at.
  if (selection !== latestOcrSelection) {
    untrackChunk(chunk);
    return;
  }

  // The on-device model, if the person turned it on, reads after the standard
  // pass so the box fills quickly either way. It only runs for the selection
  // that is still current and only when the standard pass produced text to
  // improve on.
  const pagesToRead = pieces.filter((piece) => piece.read !== undefined).length;
  if (isAdvancedOCREnabled() && pagesToRead > 0 && selection === latestOcrSelection && ocrChars > 0) {
    void improveWithOnDeviceModel(selection, chunk, pieces, editsAtStart);
  } else {
    untrackChunk(chunk);
  }

  // "Threw" is not the only failure. Every engine can return cleanly and
  // still yield nothing for a photo too blurry to read, which looks
  // identical to the user: an empty box under a form that says a file is
  // enough. Treat "produced no text" as a failure too.
  const producedText = ocrChars > 0;
  if (failures > 0 && producedText) {
    // Some pages read and some did not. Saying "we couldn't read your file"
    // here would be plainly false with their text sitting right below it.
    notePartialOcrFailure();
  } else if (failures > 0 || !producedText) {
    noteOcrFailure();
  }
};

// The library's cache bucket, where the model's weights live after the first
// download. Named here only so the person can remove them.
const ON_DEVICE_MODEL_CACHE = "transformers-cache";

// Asked before the stored model is deleted, in the browser's plain OK/Cancel
// dialog: what goes, what stays, and that it comes back if wanted.
const REMOVE_MODEL_CONFIRM =
  "Remove the downloaded model (about 760 MB) from this device? Your text and uploaded files are not touched. " +
  "If you scan with the option on again later, the model downloads again.";

// The most one page may take, first download included: a 760 MB download on
// a slow connection plus a read on a modest GPU. Past this the page is given
// up and the model switched off for the rest of the visit, because a
// download interrupted by a network change left the library's load pending
// forever and with it the pass, the lock, and the remove control (page
// check).
const ON_DEVICE_PAGE_TIMEOUT_MS = 10 * 60_000;

// Past the budget the model is given up and the person is told at once, but
// the read (and with it the pass and the lock) ends only when the running
// generation has actually stopped: the library checks its stop switch
// between forward passes, and a pass still pending on the GPU is not
// cancelled by anything, so the lock cannot be handed to another tab
// while it runs (review). A GPU that never answers keeps this tab's lock
// until the tab closes, which is the truth of the matter.
const TIMED_OUT_STATUS = "The on-device model did not finish in time; the standard reading stays.";

function readWithTimeout(read: () => Promise<string>, onTimeout: () => void): Promise<string> {
  return new Promise<string>((resolve, reject) => {
    const timer = window.setTimeout(() => {
      giveUpOnDeviceModel();
      onTimeout();
      void onDeviceGenerationSettled().then(() => {
        reject(new Error("the on-device model did not finish in time"));
      });
    }, ON_DEVICE_PAGE_TIMEOUT_MS);
    read().then(
      (text) => {
        window.clearTimeout(timer);
        resolve(text);
      },
      (error) => {
        window.clearTimeout(timer);
        reject(error);
      },
    );
  });
}

// One page as the standard pass put it in the box, with the model's read of
// it when there is one. The order of these IS the layout of the selection's
// text: piece n sits right after piece n-1.
interface EmittedPiece {
  text: string;
  read?: OnDeviceRead;
}

// Where a selection's text sits in the box, followed by POSITION. The
// standard pass appends pages at the end; the person may type before, after
// or inside them while it runs and while the model reads. Every keystroke is
// one contiguous replacement, so: an edit before the range shifts it, an edit
// after it leaves it alone, an edit inside it marks it dirty, after which
// the model's reading is only ever offered as an addition. No text is ever
// located by searching for it: an identical earlier upload made a search
// find the wrong copy (review).
interface TrackedChunk {
  start: number;
  end: number;
  dirty: boolean;
}

const trackedChunks = new Set<TrackedChunk>();
// The box as this script last saw it; the next input event is diffed
// against it.
let lastKnownValue = "";
// Typing by the person, as opposed to text this script puts in the box. Any
// typing since a pass began turns the automatic swap into an offer.
let userEditsSeen = 0;

function trackRange(start: number, end: number): TrackedChunk {
  const chunk = { start, end, dirty: false };
  trackedChunks.add(chunk);
  return chunk;
}

function trackChunk(at: number): TrackedChunk {
  return trackRange(at, at);
}

function untrackChunk(chunk: TrackedChunk): void {
  trackedChunks.delete(chunk);
}

// A page the standard pass appended extends its own chunk, as long as the
// chunk still reaches the end of the box. If the person typed after it, the
// pages are no longer one run and positions inside it cannot be trusted.
function noteProgrammaticAppend(chunk: TrackedChunk, wasLength: number, value: string): void {
  if (chunk.end === wasLength) {
    chunk.end = value.length;
  } else {
    chunk.dirty = true;
  }
  lastKnownValue = value;
}

// Brings the tracker up to date with the box. Called from the input
// listener, and again before every decision, because not every write comes
// through an input event: the Remove PII button assigns the value directly
// (review). Whatever changed since the last look is treated as one
// contiguous replacement and counted as the person's edit.
function syncTracker(value: string): void {
  const previous = lastKnownValue;
  lastKnownValue = value;
  if (previous === value) {
    return;
  }
  userEditsSeen += 1;
  // The edit as one contiguous replacement: what is left after the common
  // prefix and suffix are taken off. When an edit is ambiguous (typing "s."
  // right after text that ends in "s.", deleting one of two identical
  // copies) this picks the LATER position. That is safe because the swap
  // only ever happens where the text at the tracked range is exactly what
  // the standard pass emitted: every reading of an ambiguous edit yields
  // the same box, so the swap yields the same box under all of them, and
  // the only text that can be replaced is text identical to the standard
  // reading. Widening the region to every position the edit could have
  // had was tried and marked the commonest case dirty: a note typed right
  // after a page whose last characters the note happened to share (page
  // check).
  const shortest = Math.min(previous.length, value.length);
  let prefix = 0;
  while (prefix < shortest && previous[prefix] === value[prefix]) {
    prefix += 1;
  }
  let suffix = 0;
  while (
    suffix < shortest - prefix &&
    previous[previous.length - 1 - suffix] === value[value.length - 1 - suffix]
  ) {
    suffix += 1;
  }
  const editStart = prefix;
  const editEnd = previous.length - suffix;
  const delta = value.length - previous.length;
  trackedChunks.forEach((chunk) => {
    if (chunk.dirty) {
      return;
    }
    if (editEnd <= chunk.start) {
      chunk.start += delta;
      chunk.end += delta;
    } else if (editStart < chunk.end) {
      chunk.dirty = true;
    }
  });
}

// Every input event counts as the person's edit, whether or not the value
// ends up different (retyping the same word over a selection is still their
// hand in the box, review); the sync then follows whatever changed.
function noteUserInput(value: string): void {
  userEditsSeen += 1;
  syncTracker(value);
}

function followDenialText(textarea: HTMLTextAreaElement): void {
  lastKnownValue = textarea.value;
  textarea.addEventListener("input", () => noteUserInput(textarea.value));
}

// The model's passes and the remove control share one lock, taken in turn:
// two uploads in a row would otherwise run two generations on the GPU at
// once, and a removal could otherwise race a load that refills the bucket
// behind it (review). The count is how many passes are queued or reading.
let onDeviceModelLock: Promise<void> = Promise.resolve();
let onDevicePassesActive = 0;
// The cache bucket is shared by every tab of this origin, so the lock is
// cross-tab where the browser offers one (review: a removal in one tab
// raced a load in another). Without Web Locks it is this tab's only.
const ON_DEVICE_MODEL_LOCK = "fhi-on-device-ocr-model";

function crossTabLocks(): LockManager | undefined {
  return typeof navigator !== "undefined" ? navigator.locks : undefined;
}

async function withOnDeviceModel(work: () => Promise<void>): Promise<void> {
  const locks = crossTabLocks();
  if (locks) {
    await locks.request(ON_DEVICE_MODEL_LOCK, async () => {
      await work();
    });
    return;
  }
  const previous = onDeviceModelLock;
  let release: () => void = () => undefined;
  onDeviceModelLock = new Promise<void>((resolve) => {
    release = resolve;
  });
  try {
    await previous;
    await work();
  } finally {
    release();
  }
}

// Like withOnDeviceModel, but refuses (false) instead of waiting when
// another tab holds the model: a removal must not sit for minutes behind a
// read the person cannot see.
async function tryWithOnDeviceModel(work: () => Promise<void>): Promise<boolean> {
  const locks = crossTabLocks();
  if (locks) {
    const held: unknown = await locks.request(ON_DEVICE_MODEL_LOCK, { ifAvailable: true }, async (lock) => {
      if (!lock) {
        return false;
      }
      await work();
      return true;
    });
    return held === true;
  }
  await withOnDeviceModel(work);
  return true;
}

function clearOnDeviceStatus(): void {
  const status = document.getElementById("advanced_ocr_status");
  if (!status) return;
  status.textContent = "";
  status.hidden = true;
}

function onDeviceStatus(message: string, action?: { label: string; run: () => void }): void {
  const status = document.getElementById("advanced_ocr_status");
  if (!status) return;
  status.textContent = message;
  if (action) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn-link btn-sm p-0 ms-1";
    button.textContent = action.label;
    button.addEventListener("click", () => {
      action.run();
    });
    status.append(" ", button);
  }
  status.hidden = false;
}

// The control's own label, so it can be put back after a removal message
// once the model has been downloaded again.
let removeModelLabel = "";

async function showRemoveModelControl(): Promise<void> {
  const button = document.getElementById("advanced_ocr_remove_model") as HTMLButtonElement | null;
  if (!button || typeof caches === "undefined") return;
  try {
    if (await caches.has(ON_DEVICE_MODEL_CACHE)) {
      // Downloaded (again): whatever the last click said, the model is
      // here and removable (review: after a removal the control stayed
      // disabled saying Removed while the bucket had refilled).
      button.disabled = false;
      button.textContent = removeModelLabel || button.textContent;
      button.hidden = false;
    }
  } catch {
    // Cache Storage can be unavailable (private windows, blocked storage).
  }
}

function initRemoveModelControl(): void {
  const button = document.getElementById("advanced_ocr_remove_model") as HTMLButtonElement | null;
  if (!button) return;
  removeModelLabel = button.textContent ?? "";
  button.addEventListener("click", async () => {
    if (onDevicePassesActive > 0) {
      button.textContent = "The model is reading right now; remove it when that finishes.";
      return;
    }
    // Their call, made with the facts in front of them; Cancel changes
    // nothing.
    if (!window.confirm(REMOVE_MODEL_CONFIRM)) {
      return;
    }
    button.disabled = true;
    button.textContent = "Removing…";
    try {
      // Under the same lock as the model's passes: a read that starts now
      // waits its turn and cannot refill the bucket behind this.
      let gone = false;
      const held = await tryWithOnDeviceModel(async () => {
        await caches.delete(ON_DEVICE_MODEL_CACHE);
        gone = !(await caches.has(ON_DEVICE_MODEL_CACHE));
      });
      if (!held) {
        button.textContent = "The model is in use in another tab; remove it when that finishes.";
        button.disabled = false;
      } else if (gone && onDeviceLoadMayStillBeRunning()) {
        // A failed or abandoned load, in this tab or another, can leave the
        // library's own downloads running; they finish on their own and can
        // put files back (review).
        button.textContent =
          "Removed. A download that was already under way, here or in another tab, may leave files behind until those tabs are reloaded.";
      } else if (gone) {
        button.textContent = "Removed. It downloads again the next time you scan with this option on.";
      } else {
        button.textContent = "Could not remove it; try again when the model is not in use.";
        button.disabled = false;
      }
    } catch {
      button.textContent = "Could not remove it; your browser's site data settings can.";
      button.disabled = false;
    }
  });
  void showRemoveModelControl();
}

// The on-device model's pass: one page at a time, after the standard engines
// have filled the box, each page's reading replacing exactly that page's
// standard text by position inside this selection's chunk. If the person
// has not typed since the pass began, the swap happens with an Undo; if they
// have, it is offered as a button. The swap is made only where the tracker
// says the chunk is AND the text there is exactly what the standard pass put
// out; anything else gets an append offer, never a guess. A page the model
// cannot read keeps its standard text, a superseded selection is dropped
// quietly, and a control from an earlier selection does nothing.
async function improveWithOnDeviceModel(
  selection: number,
  chunk: TrackedChunk,
  pieces: EmittedPiece[],
  editsAtStart: number,
): Promise<void> {
  const textarea = document.getElementById("denial_text") as HTMLTextAreaElement;
  const pages = pieces.filter((piece) => piece.read !== undefined).length;
  const pageWord = pages === 1 ? "this page" : `these ${pages} pages`;
  onDeviceStatus(
    `Reading ${pageWord} again with the on-device model. This takes a minute or two per page; ` +
      "you can keep going, and the text updates when it is done.",
  );
  const readings = new Map<EmittedPiece, string>();
  let timedOut = false;
  onDevicePassesActive += 1;
  try {
    await withOnDeviceModel(async () => {
      for (const piece of pieces) {
        if (!piece.read) continue;
        if (selection !== latestOcrSelection) return;
        try {
          const text = (
            await readWithTimeout(piece.read.run, () => {
              timedOut = true;
              // Only the current selection's status is this pass's to write.
              if (selection === latestOcrSelection) {
                onDeviceStatus(TIMED_OUT_STATUS);
              }
            })
          ).trim();
          if (text) readings.set(piece, text);
        } catch (error) {
          console.warn("[QwenOCR] a page failed; keeping the standard reading for it", error);
        }
      }
    });
  } finally {
    onDevicePassesActive -= 1;
  }
  // Before the superseded check: the model was downloaded either way, so
  // the control to remove it must show either way (review).
  void showRemoveModelControl();
  if (selection !== latestOcrSelection) {
    untrackChunk(chunk);
    return;
  }
  if (readings.size === 0) {
    untrackChunk(chunk);
    // A load that failed (a stalled or broken download, most often a
    // network change mid-way) is worth a reload: the files already fetched
    // are in the cache. A read that failed on a loaded model is not.
    onDeviceStatus(
      timedOut
        ? TIMED_OUT_STATUS
        : onDeviceModelLoadFailed()
          ? "The on-device model could not be loaded; reload the page to try again. The standard reading stays."
          : "The on-device model could not read this file; the standard reading stays.",
    );
    return;
  }
  // What the standard pass put out, and the same with each read page's
  // standard text swapped for the model's reading. A page's standard text
  // is the start of its piece (a PDF page's own sparse text layer, if any,
  // follows it and stays).
  const standardText = pieces.map((piece) => piece.text).join("");
  const improvedText = pieces
    .map((piece) => {
      const reading = readings.get(piece);
      if (!reading || !piece.read || !piece.text.startsWith(piece.read.standard)) {
        return piece.text;
      }
      const tail = piece.text.slice(piece.read.standard.length);
      // A PDF page's own text layer is exact; if neither the reading nor
      // what already follows the standard text reproduces it, it stays,
      // right after the reading (review: a model reading "10/15" would
      // otherwise have replaced a known-exact "10/1").
      const exact = piece.read.exact ?? "";
      const keepExact = exact.length > 0 && !containsNormalised(reading, exact) && !containsNormalised(tail, exact);
      return reading + (keepExact ? "\n" + exact : "") + tail;
    })
    .join("");
  // No synthetic input event: that handler hides the "part of your file could
  // not be read" warning, which the model's reading of OTHER pages does not
  // resolve (review). Persisted the way typing is.
  const setValue = (value: string): void => {
    textarea.value = value;
    lastKnownValue = value;
    try {
      setLocalStorageItemWithTTL(textarea.id, value);
    } catch (error) {
      // Storage full or blocked: the box is already updated and the Undo
      // that follows must still be installed (review).
      console.warn("[QwenOCR] could not persist the draft", error);
    }
  };
  // Undo puts the standard text back where the reading went, followed
  // through any typing since; if the person edited inside it, Undo is
  // skipped rather than take their edits with it (review).
  const undoable = (start: number, applied: string, restore: string): void => {
    const placed = trackRange(start, start + applied.length);
    onDeviceStatus("Replaced with the on-device model's reading.", {
      label: "Undo",
      run: () => {
        if (selection !== latestOcrSelection) {
          clearOnDeviceStatus();
          return;
        }
        syncTracker(textarea.value);
        untrackChunk(placed);
        const current = textarea.value;
        if (placed.dirty || current.slice(placed.start, placed.end) !== applied) {
          onDeviceStatus("You changed the text since, so it was left as it is.");
          return;
        }
        setValue(current.slice(0, placed.start) + restore + current.slice(placed.end));
        onDeviceStatus("Back to the standard reading.");
      },
    });
  };
  const offerAppend = (): void => {
    onDeviceStatus("You changed the text it read, so its reading was not applied.", {
      label: "Add its reading below",
      run: () => {
        if (selection !== latestOcrSelection) {
          clearOnDeviceStatus();
          return;
        }
        const current = textarea.value;
        const separator = current.length === 0 || current.endsWith("\n") ? "" : "\n";
        const appended = separator + Array.from(readings.values()).join("\n\n") + "\n";
        setValue(current + appended);
        undoable(current.length, appended, "");
      },
    });
  };
  const apply = (): void => {
    if (selection !== latestOcrSelection) {
      clearOnDeviceStatus();
      return;
    }
    syncTracker(textarea.value);
    const previous = textarea.value;
    untrackChunk(chunk);
    if (chunk.dirty || previous.slice(chunk.start, chunk.end) !== standardText) {
      offerAppend();
      return;
    }
    setValue(previous.slice(0, chunk.start) + improvedText + previous.slice(chunk.end));
    undoable(chunk.start, improvedText, standardText);
  };
  syncTracker(textarea.value);
  const untouched =
    userEditsSeen === editsAtStart &&
    !chunk.dirty &&
    textarea.value.slice(chunk.start, chunk.end) === standardText;
  if (untouched) {
    apply();
  } else {
    onDeviceStatus("The on-device model finished reading.", { label: "Use its reading instead", run: apply });
  }
}

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

  // Auto-detect capabilities and update the advanced OCR checkbox default.
  initRemoveModelControl();
  initAdvancedOCRCheckbox().catch((err) => {
    console.warn("[AdvancedOCR] Could not determine default state:", err);
  });

  // Restore previous local values
  // Don't log the nodes themselves: the inputs hold name/address/email values.
  var nodes: NodeListOf<HTMLInputElement> = document.querySelectorAll("input");
  console.debug("scrub: input nodes found", nodes.length);
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
      // After the restore, so the restored text is the baseline the first
      // keystroke is diffed against.
      if (textarea.id === "denial_text") followDenialText(textarea);
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
