import { pdfjsLib, workers_path } from "./shared";
import {
  PDFDocumentProxy,
  TextContent,
  TextItem,
} from "pdfjs-dist/types/src/display/api";

// Tesseract
import Tesseract from "tesseract.js";
import { recognizeWithQwenWebGPU } from "./qwen_webgpu_ocr";

// How long to keep waiting for the OTHER engines once one has produced usable
// text. Qwen is a vision model reading a whole page and 10s often cut it off,
// so we threw away the better answer; but this delay is paid PER PAGE before
// anything is appended, so a very long grace turns a ten-page scan into
// minutes of blank box. 15s is the compromise.
const OCR_GRACE_PERIOD_MS = 15_000;

// Absolute ceiling for one image, whatever the engines are doing. Without it a
// single hung engine stalls the read forever: nothing else can complete the
// batch, and the user sits on a spinner with no failure ever reported.
const OCR_TOTAL_BUDGET_MS = 90_000;

// pdf.js renders at 72 DPI when scale is 1. Tesseract wants roughly a 20px
// x-height, i.e. about 300 DPI for body text; below ~150 DPI accuracy falls
// off sharply. Rendering a scan at scale 1 does not merely produce a
// low-resolution image, it DOWNSAMPLES pixels the PDF already contains.
const PDF_BASE_DPI = 72;
const OCR_TARGET_DPI = 300;

// Browsers cap a single canvas dimension independently of total area. 8192 is
// the smallest limit in common use.
const MAX_CANVAS_EDGE_PX = 8192;

// A PDF text layer holding only a scanner stamp ("Page 1 of 3") used to clear
// the old 10-character test, so OCR never ran and the user was handed that
// stamp as their entire denial. Judge DENSITY per page instead: real letters
// carry hundreds of characters a page, boilerplate carries tens.
const MIN_TEXT_LAYER_CHARS_PER_PAGE = 200;

/**
 * How many canvas pixels we are willing to allocate for one page.
 *
 * This is a correctness guard, not just a performance one. Safari on iOS caps
 * total canvas area and silently hands back a BLANK canvas past it -- which
 * would look exactly like OCR failing, the very thing we are fixing. So the
 * scale we want is always clamped to fit here.
 */
function canvasAreaBudget(): number {
  const nav = navigator as Navigator & {
    deviceMemory?: number;
    maxTouchPoints?: number;
  };
  // iPadOS reports as MacIntel, so touch points are what actually identify it.
  const isIOS =
    /iPad|iPhone|iPod/.test(navigator.userAgent) ||
    (navigator.platform === "MacIntel" && (nav.maxTouchPoints ?? 0) > 1);
  if (isIOS) {
    // Comfortably under the ~5MP cap older iOS devices enforce.
    return 4_000_000;
  }
  const memoryGb = nav.deviceMemory;
  if (typeof memoryGb === "number" && memoryGb <= 4) {
    return 8_000_000;
  }
  return 24_000_000;
}

/**
 * The scale to rasterise a page at: enough for OCR to read it, capped so the
 * canvas stays within budget.
 *
 * This may return LESS than 1. Refusing to go below 1 would have defeated the
 * clamp exactly when it matters -- a page whose natural size already exceeds
 * the budget would allocate past it and, on iOS, come back blank.
 */
function ocrRenderScale(baseWidth: number, baseHeight: number): number {
  const baseArea = baseWidth * baseHeight;
  if (!(baseArea > 0)) {
    return 1;
  }
  const desired = OCR_TARGET_DPI / PDF_BASE_DPI;
  const byArea = Math.sqrt(canvasAreaBudget() / baseArea);
  // Browsers also cap a single canvas dimension; a very long or very wide page
  // can breach that while still fitting the area budget.
  const byEdge = Math.min(
    MAX_CANVAS_EDGE_PX / baseWidth,
    MAX_CANVAS_EDGE_PX / baseHeight,
  );
  const scale = Math.min(desired, byArea, byEdge);
  // Guard against a degenerate page collapsing the canvas to nothing.
  return scale > 0 ? scale : 1;
}

// The one string the caller matches on to report "we couldn't read it".
const NO_USABLE_OCR = "All OCR engines failed or timed out";

interface EngineText {
  name: string;
  text: string;
}

interface OCRResults {
  results: EngineText[];
}

type SettledState<T> =
  | { status: "fulfilled"; value: T }
  | { status: "rejected"; reason: unknown };

interface OCREngine {
  name: string;
  promise: Promise<string>;
}

function isUsableOCRState(state: SettledState<string>): boolean {
  return state.status === "fulfilled" && state.value.trim().length > 0;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

/**
 * Run every available OCR engine against one image and collect what they
 * produce.
 *
 *  - wait for the first engine to return USABLE text (not merely the first to
 *    settle: an engine that is absent returns "" instantly, and treating that
 *    as "the first result" made us commit to a branch before anyone had read
 *    anything);
 *  - then give the stragglers ONE shared grace window, because a slower engine
 *    is often the better one and merging beats taking the fast answer;
 *  - and bound the whole thing, so a hung engine cannot stall the read
 *    forever with no failure ever surfacing.
 *
 * Throws only when every engine failed or produced nothing, which is the
 * signal the caller turns into "we couldn't read your file".
 */
async function runOCREnginesWithGrace(
  engines: OCREngine[],
): Promise<OCRResults> {
  if (engines.length === 0) {
    throw new Error(NO_USABLE_OCR);
  }

  const states = new Map<string, SettledState<string>>();
  let announceUsable: () => void = () => {};
  const sawUsable = new Promise<void>((resolve) => {
    announceUsable = resolve;
  });

  const settling = engines.map((engine) =>
    engine.promise.then(
      (value) => {
        states.set(engine.name, { status: "fulfilled", value });
        if (value.trim().length > 0) {
          announceUsable();
        }
      },
      (reason) => {
        states.set(engine.name, { status: "rejected", reason });
      },
    ),
  );
  // Every rejection is already absorbed above, so this cannot reject.
  const allSettled = Promise.all(settling).then(() => undefined);

  const startedAt = Date.now();
  const remainingBudget = () =>
    Math.max(0, OCR_TOTAL_BUDGET_MS - (Date.now() - startedAt));

  // Someone useful, everyone finished, or we run out of patience.
  await Promise.race([
    sawUsable,
    allSettled,
    sleep(remainingBudget()).then(() => undefined),
  ]);

  if (states.size < engines.length) {
    // Stragglers get ONE shared window, not one each, and never past the
    // overall budget.
    const grace = Math.min(OCR_GRACE_PERIOD_MS, remainingBudget());
    await Promise.race([allSettled, sleep(grace).then(() => undefined)]);
  }

  const results: EngineText[] = [];
  for (const engine of engines) {
    const state = states.get(engine.name);
    if (state === undefined) {
      console.warn(`[OCR] ${engine.name} did not finish in time; ignoring it`);
      continue;
    }
    if (state.status === "rejected") {
      console.warn(`[OCR] ${engine.name} failed`, state.reason);
      continue;
    }
    if (state.value.trim().length > 0) {
      results.push({ name: engine.name, text: state.value });
    }
  }

  if (results.length === 0) {
    throw new Error(NO_USABLE_OCR);
  }

  return { results };
}

/**
 * The browser's own OCR, via the Shape Detection API.
 *
 * Where it exists this calls the platform engine (Vision on Apple, ML Kit on
 * Android), which is fast, needs no model download, and is usually better on
 * photographs than tesseract. It is not available everywhere, so it is
 * feature-detected and simply contributes nothing when absent.
 */
async function recognizeWithTextDetector(
  source: Blob | File | string,
): Promise<string> {
  const detectorCtor = (
    globalThis as unknown as {
      TextDetector?: new () => {
        detect: (image: ImageBitmapSource) => Promise<{ rawValue: string }[]>;
      };
    }
  ).TextDetector;
  if (!detectorCtor || typeof createImageBitmap !== "function") {
    return "";
  }

  let blob: Blob;
  if (typeof source === "string") {
    // recognizePDF hands us a data: URL for the rendered page.
    blob = await (await fetch(source)).blob();
  } else {
    blob = source;
  }

  const bitmap = await createImageBitmap(blob);
  try {
    const blocks = await new detectorCtor().detect(bitmap);
    return blocks
      .map((b) => b.rawValue)
      .join("\n")
      .trim();
  } finally {
    // Bitmaps hold decoded pixels; a multi-page scan leaks fast without this.
    bitmap.close();
  }
}

async function getTesseractWorkerRaw(): Promise<Tesseract.Worker> {
  console.log("Loading tesseract worker.");
  const worker = await Tesseract.createWorker("eng", 1, {
    corePath: workers_path + "tesseract.js-core",
    workerPath: workers_path + "tesseract.js/worker.min.js",
    logger: function (m) {
      console.log(m);
    },
  });
  await worker.setParameters({
    tessedit_pageseg_mode: Tesseract.PSM.AUTO_OSD,
  });
  return worker;
}

// The worker is shared, so it is held here rather than memoized: a wedged one
// has to be THROWN AWAY, and a memoizer has no way to say that.
let tesseractWorker: Promise<Tesseract.Worker> | null = null;

function getTesseractWorker(): Promise<Tesseract.Worker> {
  if (tesseractWorker === null) {
    tesseractWorker = getTesseractWorkerRaw();
  }
  return tesseractWorker;
}

/** Drop the current worker so the next job builds a fresh one. */
async function discardTesseractWorker(): Promise<void> {
  const wedged = tesseractWorker;
  tesseractWorker = null;
  if (wedged === null) {
    return;
  }
  try {
    const worker = await wedged;
    await worker.terminate();
  } catch (error) {
    // It was already broken; letting go of the reference is the point.
    console.warn("[OCR] discarding a stuck tesseract worker", error);
  }
}

// One shared worker means one queue whether we manage it or not: tesseract.js
// accepts every recognize() immediately and runs them in turn internally. That
// hid the queue from us -- a page that timed out had already handed over a
// full-resolution job, so page two waited behind page one and page three
// behind page two, all for output nobody would read.
//
// Serialising here makes the queue ours, so a job can be dropped at the moment
// the worker actually becomes free rather than being committed up front.
let tesseractQueue: Promise<unknown> = Promise.resolve();

// Longer than the per-image budget, so a slow-but-healthy read is never cut
// off; this only catches a slot that has genuinely stopped making progress.
const TESSERACT_SLOT_TIMEOUT_MS = OCR_TOTAL_BUDGET_MS + 30_000;

function queueTesseractJob<T>(job: () => Promise<T>): Promise<T> {
  const run = tesseractQueue.then(job, job);
  // The chain advances on success, on failure, OR when a slot stops
  // responding. Without that last case one hung recognition -- or one hung
  // worker startup -- left the chain permanently PENDING, and every later
  // page, and every later upload in the same tab, waited behind it forever.
  // Advancing alone would not be enough: the next job would be handed to the
  // still-wedged worker and rebuild the same hidden queue, so the worker is
  // discarded and the next job starts a fresh one.
  tesseractQueue = Promise.race([
    run.then(
      () => undefined,
      () => undefined,
    ),
    sleep(TESSERACT_SLOT_TIMEOUT_MS).then(() => discardTesseractWorker()),
  ]);
  return run;
}

function isPDF(file: File): boolean {
  return (
    file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf")
  );
}

async function getFileAsArrayBuffer(file: File): Promise<Uint8Array> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();

    reader.onload = () => {
      if (reader.result instanceof ArrayBuffer) {
        resolve(new Uint8Array(reader.result));
      } else {
        reject(new Error("Unexpected result type from FileReader"));
      }
    };

    reader.onerror = () => {
      reject(reader.error);
    };

    reader.readAsArrayBuffer(file);
  });
}

// How much we trust an engine when two disagree and neither contains the
// other. Tesseract and Qwen read the whole page as a document; TextDetector
// returns detected blocks, which can come back longer than a correct read
// while being more fragmented -- taking the longest string outright would let
// it displace an accurate result the two-engine version would have kept.
// Qwen and Tesseract are PEERS: both read the whole page as a document, and
// neither is reliably better. Ranking Qwen above Tesseract would let a short
// vision-model hallucination ("This appears to be a denial") replace a
// complete transcription -- worse than the two-engine rule it replaced, which
// took the longer text. Only TextDetector sits lower, because it returns
// detected blocks that can be longer while more fragmented.
const ENGINE_PRECEDENCE: Record<string, number> = {
  qwen: 2,
  tesseract: 2,
  "text-detector": 1,
};

/**
 * Is `needle` genuinely reproduced inside `haystack`?
 *
 * Whitespace is normalised because a PDF text layer joins runs with spaces
 * while OCR emits line breaks, and calling those different re-appended the
 * whole sparse layer.
 *
 * But a plain substring test is unsafe in the other direction, and unsafely on
 * exactly the content that matters: "Appeal by 10/1" IS a substring of an OCR
 * guess of "Appeal by 10/15", so the exact deadline would be discarded in
 * favour of the wrong one. Claim and member numbers fail the same way. So the
 * match must end on a token boundary -- if the haystack continues the token,
 * these are different values and the exact one is kept.
 */
function containsNormalised(haystack: string, needle: string): boolean {
  const flatten = (t: string) => t.replace(/\s+/g, " ").trim();
  const flatNeedle = flatten(needle);
  if (flatNeedle.length === 0) {
    return true;
  }
  const flatHay = flatten(haystack);

  const isWordChar = (c: string | undefined) =>
    c !== undefined && /[A-Za-z0-9]/.test(c);
  const startsToken = isWordChar(flatNeedle[0]);
  const endsToken = isWordChar(flatNeedle[flatNeedle.length - 1]);

  let from = 0;
  for (;;) {
    const at = flatHay.indexOf(flatNeedle, from);
    if (at === -1) {
      return false;
    }
    const before = at > 0 ? flatHay[at - 1] : undefined;
    const after = flatHay[at + flatNeedle.length];
    const openOk = !startsToken || !isWordChar(before);
    const closeOk = !endsToken || !isWordChar(after);
    if (openOk && closeOk) {
      return true;
    }
    from = at + 1;
  }
}

function precedenceOf(name: string): number {
  return ENGINE_PRECEDENCE[name] ?? 0;
}

function mergeOCRTexts({ results }: OCRResults): string {
  const candidates = results
    .map((r) => ({ name: r.name, text: r.text.trim() }))
    .filter((r) => r.text.length > 0);
  if (candidates.length === 0) {
    return "";
  }

  // Precedence FIRST, across tiers. Containment used to be checked before
  // rank, which let a fragmented TextDetector result that happened to contain
  // the tesseract text ("correct text + a duplicated block") win on
  // containment despite being the least trusted engine -- the opposite of the
  // guarantee. A supplementary engine now only speaks when no primary one did.
  const bestRank = Math.max(...candidates.map((c) => precedenceOf(c.name)));
  const tier = candidates.filter((c) => precedenceOf(c.name) === bestRank);

  // Within one tier, the original two-engine rule: a superset is strictly
  // more of the same letter, otherwise take the longer read.
  let best = tier[0];
  for (const candidate of tier.slice(1)) {
    if (candidate.text.includes(best.text)) {
      best = candidate;
    } else if (
      !best.text.includes(candidate.text) &&
      candidate.text.length > best.text.length
    ) {
      best = candidate;
    }
  }
  return best.text;
}

function isAdvancedOCREnabled(): boolean {
  const checkbox = document.getElementById(
    "advanced_ocr_enabled",
  ) as HTMLInputElement | null;
  // Default to true when the checkbox is absent (non-scrub pages).
  return checkbox ? checkbox.checked : true;
}

async function recognizeImageText(
  file: Blob | File | string,
): Promise<OCRResults> {
  const engines: OCREngine[] = [];

  // Bounding the WAIT does not stop the work. Once a page gives up, its
  // engines keep running, and the memoized tesseract worker is shared: a
  // multi-page scan could queue one stale full-resolution recognition per
  // abandoned page onto the same worker, each still holding its data URL.
  // This lets an engine notice it has been abandoned before starting work
  // that nobody will read.
  const page = { abandoned: false };

  // Worker creation downloads trained data and can itself hang. Awaiting it
  // HERE put it outside the budget entirely: the timer had not started, no
  // other engine had been launched, and a stalled download stalled the whole
  // read with no failure ever reported. Fold it into tesseract's own promise
  // so it is raced and bounded like any other engine.
  engines.push({
    name: "tesseract",
    promise: queueTesseractJob(async () => {
      const worker = await getTesseractWorker();
      // Checked HERE, with the worker actually free and our turn arrived --
      // not when the job was created. By now this page may be long gone.
      if (page.abandoned) {
        throw new Error("page abandoned before tesseract reached it");
      }
      const result: Tesseract.RecognizeResult = await worker.recognize(file);
      return result.data.text;
    }),
  });

  if (isAdvancedOCREnabled()) {
    engines.push({ name: "qwen", promise: recognizeWithQwenWebGPU(file) });
  }

  // Free where the platform provides it; contributes "" and drops out of the
  // merge where it does not.
  engines.push({
    name: "text-detector",
    promise: recognizeWithTextDetector(file),
  });

  try {
    return await runOCREnginesWithGrace(engines);
  } finally {
    page.abandoned = true;
  }
}

const recognizePDF = async function (
  file: File,
  addText: (str: string) => void,
) {
  const typedarray = await getFileAsArrayBuffer(file);
  const loadingTask = pdfjsLib.getDocument(typedarray);
  const doc = await loadingTask.promise;
  const pageCount = doc.numPages;

  let unreadablePages = 0;
  try {
    for (let pageNo = 1; pageNo <= doc.numPages; pageNo++) {
      // Caught per page around EVERYTHING, not just the OCR call. getPage,
      // getTextContent, viewport and context creation can all fail too, and
      // one of those escaping after earlier pages had been appended reached
      // the whole-file image fallback -- re-decoding a document we had
      // already partly emitted.
      try {
        if (!(await recognizePDFPage(doc, pageNo, addText))) {
          unreadablePages += 1;
        }
      } catch (error) {
        unreadablePages += 1;
        console.warn(`[OCR] page ${pageNo} could not be read`, error);
      }
    }
  } finally {
    // pdf.js caches page proxies and rendering resources. At OCR resolution
    // that is a lot to leave behind once the batch moves on.
    //
    // Swallowed on purpose: destroy() rethrows transport-teardown failures,
    // and letting one escape here would reach recognize()'s outer catch and
    // re-decode the whole PDF through the image route -- duplicating text we
    // had already appended, or reporting failure after a clean read.
    try {
      await doc.destroy();
    } catch (error) {
      console.warn("[OCR] releasing the PDF failed; text already extracted", error);
    }
  }

  // Tell the caller a page was lost. Without this a three-page denial that
  // dropped page two reported complete success, and the missing deadline or
  // reason was never mentioned. The pages that DID read have already been
  // appended, so the caller sees text plus a failure and reports partial.
  if (unreadablePages > 0) {
    throw new Error(
      `${unreadablePages} of ${pageCount} page(s) could not be read`,
    );
  }
};

/**
 * Read ONE page: use its embedded text when that text is real, otherwise
 * rasterise and OCR it.
 *
 * The decision is per page and not per document on purpose. Averaging over the
 * document mixes the two cases and gets both wrong: one dense cover page and
 * two scanned pages averages out above the threshold, so the scans are
 * silently dropped; a shorter cover page drags the average under it, so we
 * discard exact digital text and OCR everything. Mixed documents are the
 * normal case here -- a typed cover letter stapled to a scanned denial.
 */
async function recognizePDFPage(
  doc: PDFDocumentProxy,
  pageNo: number,
  addText: (str: string) => void,
): Promise<boolean> {
  const page = await doc.getPage(pageNo);
  try {
    const pageText = (await getPDFPageText(doc, pageNo)).trim();

    // A digital page carries its own text: exact, instant, and better than
    // anything OCR will produce. Only boilerplate falls through -- a scanner
    // stamping "Page 1 of 3" cleared the old 10-character test, which
    // suppressed OCR and handed the user that stamp as their whole denial.
    if (pageText.length >= MIN_TEXT_LAYER_CHARS_PER_PAGE) {
      addText(pageText + "\n");
      return true;
    }

    const base = page.getViewport({ scale: 1.0 });
    const viewport = page.getViewport({
      scale: ocrRenderScale(base.width, base.height),
    });

    const canvas = document.createElement("canvas");
    const context = canvas.getContext("2d");
    if (!context) {
      throw new Error("Could not get 2D context for PDF page rendering");
    }
    canvas.height = viewport.height;
    canvas.width = viewport.width;

    let ocrText = "";
    let ocrFailed = false;
    try {
      await page.render({ canvasContext: context, viewport, canvas }).promise;
      ocrText = mergeOCRTexts(
        await recognizeImageText(canvas.toDataURL("image/png")),
      ).trim();
    } catch (error) {
      // Caught PER PAGE so one unreadable page cannot abandon the rest, and
      // so this page's own text layer is still available as a fallback.
      ocrFailed = true;
      console.warn(`[OCR] could not read page ${pageNo}`, error);
    } finally {
      // A multi-page scan at OCR resolution holds a lot of pixels; drop them
      // as we go rather than keeping every page alive until the loop ends.
      canvas.width = 0;
      canvas.height = 0;
    }

    // Compose the page's whole contribution and emit it with ONE addText
    // call. Callers treat each call as a separate document -- explain_denial
    // inserts a separator per call -- so emitting twice made one page look
    // like two uploads.
    const parts: string[] = [];
    if (ocrText.length > 0) {
      parts.push(ocrText);
    }
    // Keep the page's own embedded text when OCR did not reproduce it. It is
    // exact where OCR is a guess, and on a denial the sparse bits are often
    // the ones that matter: a stamped appeal deadline, a claim number.
    // Compared with whitespace collapsed, because a text layer joins runs with
    // spaces while OCR emits line breaks -- a raw comparison called those
    // different and appended the whole layer a second time.
    if (pageText.length > 0 && !containsNormalised(ocrText, pageText)) {
      parts.push(pageText);
    }
    if (parts.length > 0) {
      addText(parts.join("\n") + "\n");
    }

    // Emitting the sparse layer is NOT the same as having read the page. A
    // scanned page whose only embedded text is "Page 2 of 3" would otherwise
    // contribute that stamp and report success, and the denial content on it
    // would be lost without a word. Report on whether OCR actually read it.
    return !ocrFailed && ocrText.length > 0;
  } finally {
    page.cleanup();
  }
}

const recognizeImage = async function (
  file: File,
  addText: (str: string) => void,
) {
  const text = mergeOCRTexts(await recognizeImageText(file));
  addText(text);
};

export const recognize = async function (
  file: File,
  addText: (str: string) => void,
) {
  // Track whether anything reached the page before falling back. Re-running a
  // different decoder over a document we have already partly emitted either
  // duplicates that text or reports failure after a usable read.
  let emitted = false;
  const emit = (text: string): void => {
    if (text.trim().length > 0) {
      emitted = true;
    }
    addText(text);
  };

  if (isPDF(file)) {
    try {
      await recognizePDF(file, emit);
    } catch (error) {
      if (emitted) {
        throw error;
      }
      console.error("Error processing PDF, trying image route:", error);
      await recognizeImage(file, emit);
    }
  } else {
    try {
      await recognizeImage(file, emit);
    } catch (error) {
      if (emitted) {
        throw error;
      }
      console.error("Error processing image, trying PDF route:", error);
      await recognizePDF(file, emit);
    }
  }
};

async function getPDFPageText(
  pdf: PDFDocumentProxy,
  pageNo: number,
): Promise<string> {
  const page = await pdf.getPage(pageNo);
  const tokenizedText: TextContent = await page.getTextContent();
  const items: TextItem[] = [];
  tokenizedText.items.forEach((item) => {
    if ("str" in item) {
      items.push(item as TextItem);
    }
  });
  const strs = items.map((token: TextItem) => token.str);
  return strs.join(" ");
}

async function getPDFText(pdf: PDFDocumentProxy): Promise<string> {
  const maxPages = pdf.numPages;
  const pageTextPromises: Promise<string>[] = [];
  for (let pageNo = 1; pageNo <= maxPages; pageNo += 1) {
    pageTextPromises.push(getPDFPageText(pdf, pageNo));
  }
  const pageTexts = await Promise.all(pageTextPromises);
  return pageTexts.join(" ");
}
