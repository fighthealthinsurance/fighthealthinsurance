import * as pdfjsLib from "pdfjs-dist";

function getLocalStorageItemOrDefault(
  key: string,
  defaultValue: string,
): string {
  const value = window.localStorage.getItem(key);
  return value !== null ? value : defaultValue;
}

function getLocalStorageItemOrDefaultEQ(key: string): string {
  return getLocalStorageItemOrDefault(key, key);
}

const storeLocal = async function (event: Event) {
  const target = event.target as HTMLInputElement;
  const name = target.id;
  const value = target.value;
  window.localStorage.setItem(name, value);
};

const node_module_path = "/static/js/node_modules/";

// pdf.js
pdfjsLib.GlobalWorkerOptions.workerSrc =
  node_module_path + "pdfjs-dist/build/pdf.worker.min.js";

export {
  storeLocal,
  pdfjsLib,
  node_module_path,
  getLocalStorageItemOrDefault,
  getLocalStorageItemOrDefaultEQ,
};
