import * as pdfjsLib from 'pdfjs-dist';

const storeLocal = async function(evt) {
    const name = evt.target.id;
    const value = evt.target.value;
    window.localStorage.setItem(name, value);
}

// pdf.js
pdfjsLib.GlobalWorkerOptions.workerSrc = node_module_path + "pdfjs-dist/build/pdf.worker.js";

export { storeLocal, pdfjsLib };
