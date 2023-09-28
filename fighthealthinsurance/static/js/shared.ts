import * as pdfjsLib from 'pdfjs-dist';

function getLocalStorageItemOrDefault(key, defaultValue) {
    const value = window.localStorage.getItem(key);
    return value !== null ? value : defaultValue;
}

function getLocalStorageItemOrDefaultEQ(key) {
    return getLocalStorageItemOrDefault(key, key);
}


const storeLocal = async function(evt) {
    const name = evt.target.id;
    const value = evt.target.value;
    window.localStorage.setItem(name, value);
}

const node_module_path = "/static/js/node_modules/";

// pdf.js
pdfjsLib.GlobalWorkerOptions.workerSrc = node_module_path + "pdfjs-dist/build/pdf.worker.js";

export { storeLocal, pdfjsLib, node_module_path,  getLocalStorageItemOrDefault, getLocalStorageItemOrDefaultEQ};
