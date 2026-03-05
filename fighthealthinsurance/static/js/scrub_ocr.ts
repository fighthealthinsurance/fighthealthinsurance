import { pdfjsLib, node_module_path } from "./shared";
import {
  PDFDocumentProxy,
  TextContent,
  TextItem,
} from "pdfjs-dist/types/src/display/api";

// Tesseract
import Tesseract from "tesseract.js";
import { recognizeWithQwenWebGPU } from "./qwen_webgpu_ocr";

const OCR_GRACE_PERIOD_MS = 10_000;

interface OCRResults {
  tesseractText: string;
  qwenText: string;
}

interface OCRRunnerOutcome {
  tesseractText: string;
  qwenText: string;
  usedFallback: boolean;
}

type SettledState<T> =
  | { status: "fulfilled"; value: T }
  | { status: "rejected"; reason: unknown };

function isUsableOCRState(state: SettledState<string>): boolean {
  return state.status === "fulfilled" && state.value.trim().length > 0;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

async function runDualOCRWithGrace(
  tesseractPromise: Promise<string>,
  qwenPromise: Promise<string>,
): Promise<OCRRunnerOutcome> {
  const trackedTesseract: Promise<SettledState<string>> = tesseractPromise.then(
    (value) => ({ status: "fulfilled", value }),
    (reason) => ({ status: "rejected", reason }),
  );

  const trackedQwen: Promise<SettledState<string>> = qwenPromise.then(
    (value) => ({ status: "fulfilled", value }),
    (reason) => ({ status: "rejected", reason }),
  );

  const firstSettled = await Promise.race([
    trackedTesseract.then((state) => ({ who: "tesseract" as const, state })),
    trackedQwen.then((state) => ({ who: "qwen" as const, state })),
  ]);

  let tesseractState: SettledState<string> | null = null;
  let qwenState: SettledState<string> | null = null;

  if (firstSettled.who === "tesseract") {
    tesseractState = firstSettled.state;

    if (isUsableOCRState(tesseractState)) {
      const qwenResult = await Promise.race([
        trackedQwen
          .catch((reason): SettledState<string> => ({ status: "rejected", reason }))
          .then((state) => ({ timedOut: false as const, state })),
        sleep(OCR_GRACE_PERIOD_MS).then(() => ({ timedOut: true as const })),
      ]);

      if (!qwenResult.timedOut && isUsableOCRState(qwenResult.state)) {
        qwenState = qwenResult.state;
      }
    } else {
      qwenState = await trackedQwen.catch(
        (reason): SettledState<string> => ({ status: "rejected", reason }),
      );
    }
  } else {
    qwenState = firstSettled.state;

    if (isUsableOCRState(qwenState)) {
      const tesseractResult = await Promise.race([
        trackedTesseract
          .catch((reason): SettledState<string> => ({ status: "rejected", reason }))
          .then((state) => ({ timedOut: false as const, state })),
        sleep(OCR_GRACE_PERIOD_MS).then(() => ({ timedOut: true as const })),
      ]);

      if (!tesseractResult.timedOut && isUsableOCRState(tesseractResult.state)) {
        tesseractState = tesseractResult.state;
      }
    } else {
      // If Qwen settles first but with empty/error output, do NOT time-box baseline OCR.
      tesseractState = await trackedTesseract.catch(
        (reason): SettledState<string> => ({ status: "rejected", reason }),
      );
    }
  }

  if (tesseractState?.status === "rejected") {
    console.warn("[TesseractOCR] Primary OCR path failed", tesseractState.reason);
  }

  if (qwenState?.status === "rejected") {
    console.warn("[QwenOCR] Parallel OCR path failed; using Tesseract result", qwenState.reason);
  }

  if (qwenState === null) {
    console.warn(
      `[QwenOCR] Timed out after ${OCR_GRACE_PERIOD_MS}ms grace period; using available OCR result`,
    );
  }

  const tesseractText = tesseractState?.status === "fulfilled" ? tesseractState.value : "";
  const qwenText = qwenState?.status === "fulfilled" ? qwenState.value : "";

  if (!tesseractText && !qwenText) {
    throw new Error("All OCR engines failed or timed out");
  }

  return {
    tesseractText,
    qwenText,
    usedFallback: !tesseractText && !!qwenText,
  };
}

async function getTesseractWorkerRaw(): Promise<Tesseract.Worker> {
  console.log("Loading tesseract worker.");
  const worker = await Tesseract.createWorker("eng", 1, {
    corePath: node_module_path + "/tesseract.js-core",
    workerPath: node_module_path + "/tesseract.js/dist/worker.min.js",
    logger: function (m) {
      console.log(m);
    },
  });
  await worker.setParameters({
    tessedit_pageseg_mode: Tesseract.PSM.AUTO_OSD,
  });
  return worker;
}

// eslint-disable-next-line @typescript-eslint/no-require-imports
const memoizeOne = require("async-memoize-one");
const getTesseractWorker = memoizeOne(getTesseractWorkerRaw);

function isPDF(file: File): boolean {
  return file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf");
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

function mergeOCRTexts({ tesseractText, qwenText }: OCRResults): string {
  const cleanTesseract = tesseractText.trim();
  const cleanQwen = qwenText.trim();

  if (!cleanTesseract) {
    return cleanQwen;
  }

  if (!cleanQwen) {
    return cleanTesseract;
  }

  if (cleanQwen.includes(cleanTesseract)) {
    return cleanQwen;
  }

  if (cleanTesseract.includes(cleanQwen)) {
    return cleanTesseract;
  }

  return cleanQwen.length > cleanTesseract.length ? cleanQwen : cleanTesseract;
}

function isAdvancedOCREnabled(): boolean {
  const checkbox = document.getElementById("advanced_ocr_enabled") as HTMLInputElement | null;
  // Default to true when the checkbox is absent (non-scrub pages).
  return checkbox ? checkbox.checked : true;
}

async function recognizeImageText(file: Blob | File | string): Promise<OCRResults> {
  const worker = await getTesseractWorker();
  const tesseractPromise = worker
    .recognize(file)
    .then((result: Tesseract.RecognizeResult) => result.data.text);
  const qwenPromise = isAdvancedOCREnabled() ? recognizeWithQwenWebGPU(file) : Promise.resolve("");
  const result = await runDualOCRWithGrace(tesseractPromise, qwenPromise);

  if (result.usedFallback) {
    console.warn("[TesseractOCR] Falling back to Qwen OCR text because Tesseract output was unavailable");
  }

  return {
    tesseractText: result.tesseractText,
    qwenText: result.qwenText,
  };
}

const recognizePDF = async function (
  file: File,
  addText: (str: string) => void,
) {
  const typedarray = await getFileAsArrayBuffer(file);
  const loadingTask = pdfjsLib.getDocument(typedarray);
  const doc = await loadingTask.promise;
  const pdfText = await getPDFText(doc);
  addText(pdfText);

  // Did we have almost no text? Try OCR
  if (pdfText.trim().length < 10) {
    const numPages = doc.numPages;

    for (let pageNo = 1; pageNo <= numPages; pageNo++) {
      const page = await doc.getPage(pageNo);
      const viewport = page.getViewport({ scale: 1.0 });

      const canvas = document.createElement("canvas");
      const context = canvas.getContext("2d");
      if (!context) {
        throw new Error("Could not get 2D context for PDF page rendering");
      }

      canvas.height = viewport.height;
      canvas.width = viewport.width;

      await page.render({ canvasContext: context, viewport, canvas }).promise;

      const imageData = canvas.toDataURL("image/png");
      const mergedText = mergeOCRTexts(await recognizeImageText(imageData));
      addText(mergedText + "\n");
    }
  }
};

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
  if (isPDF(file)) {
    try {
      await recognizePDF(file, addText);
    } catch (error) {
      console.error("Error processing PDF, trying image route:", error);
      await recognizeImage(file, addText);
    }
  } else {
    try {
      await recognizeImage(file, addText);
    } catch (error) {
      console.error("Error processing image, trying PDF route:", error);
      await recognizePDF(file, addText);
    }
  }
};

async function getPDFPageText(pdf: PDFDocumentProxy, pageNo: number): Promise<string> {
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
