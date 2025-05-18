import { pdfjsLib, node_module_path } from "./shared";
import { TextContent, TextItem } from "pdfjs-dist/types/src/display/api";

// Tesseract
import Tesseract from "tesseract.js";

async function getTesseractWorkerRaw(): Promise<Tesseract.Worker> {
  console.log("Loading tesseract worker.");
  const worker = await Tesseract.createWorker("eng", 1, {
    corePath: node_module_path + "/tesseract.js-core/tesseract-core.wasm.js",
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
  return file.type.match("application/pdf") !== null;
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

const recognizePDF = async function (
  file: File,
  addText: (str: string) => void,
) {
  const typedarray = await getFileAsArrayBuffer(file);
  console.log("Data?");
  console.log(typedarray);
  const loadingTask = pdfjsLib.getDocument(typedarray);
  const doc = await loadingTask.promise;
  const pdfText = await getPDFText(doc);
  addText(pdfText);
  // Did we have almost no text? Try OCR
  if (pdfText.trim().length < 10) {
    const numPages = doc.numPages;
    const worker = await getTesseractWorker();

    for (let pageNo = 1; pageNo <= numPages; pageNo++) {
      const page = await doc.getPage(pageNo);
      const viewport = page.getViewport({ scale: 1.0 });

      const canvas = document.createElement("canvas");
      const context = canvas.getContext("2d")!;
      canvas.height = viewport.height;
      canvas.width = viewport.width;

      await page.render({ canvasContext: context, viewport }).promise;

      const imageData = canvas.toDataURL("image/png");
      const ocrResult = await worker.recognize(imageData);
      addText(ocrResult.data.text + "\n");
    }
  }
};

const recognizeImage = async function (
  file: File,
  addText: (str: string) => void,
) {
  const worker = await getTesseractWorker();
  const ret = await worker.recognize(file);
  console.log("Recognize done!");
  console.log(ret.data.text);
  addText(ret.data.text);
};

export const recognize = async function (
  file: File,
  addText: (str: string) => void,
) {
  if (isPDF(file)) {
    console.log("probably pdf");
    try {
      await recognizePDF(file, addText);
    } catch {
      await recognizeImage(file, addText);
    }
  } else {
    console.log("Assuming image...");
    try {
      await recognizeImage(file, addText);
    } catch {
      await recognizePDF(file, addText);
    }
  }
};

// eslint-disable-next-line @typescript-eslint/no-explicit-any
async function getPDFPageText(pdf: any, pageNo: number): Promise<string> {
  const page = await pdf.getPage(pageNo);
  const tokenizedText: TextContent = await page.getTextContent();
  const items: TextItem[] = [];
  tokenizedText.items.forEach((item) => {
    // Type guard to ensure item is of type TextItem
    if ("str" in item) {
      items.push(item as TextItem);
    }
  });
  const strs = items.map((token: TextItem) => token.str);
  const pageText = strs.join(" ");
  return pageText;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
async function getPDFText(pdf: any): Promise<string> {
  console.log("Getting text from PDF:");
  console.log(pdf);
  const maxPages = pdf.numPages;
  console.log("Processing PDF pages:");
  console.log(maxPages);
  const pageTextPromises = [];
  for (let pageNo = 1; pageNo <= maxPages; pageNo += 1) {
    pageTextPromises.push(getPDFPageText(pdf, pageNo));
  }
  const pageTexts = await Promise.all(pageTextPromises);
  return pageTexts.join(" ");
}
