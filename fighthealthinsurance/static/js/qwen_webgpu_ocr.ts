// Optional Qwen WebGPU OCR path.
// This module must fail closed: if WebGPU/model init fails, callers keep using baseline OCR.
//
// The model is driven directly, not through pipeline(). pipeline("image-to-text")
// resolves to the vision-encoder-decoder registry, which does not hold this
// model family, and transformers.js exposes no image-text-to-text pipeline
// task, so every pipeline() call threw "Unsupported model type: qwen3_5" and
// the engine never once produced text. The working path is the one the model
// card documents: AutoProcessor with the chat template, the image resized so a
// letter page does not become tens of thousands of vision patches, the model's
// own generate(), then batch_decode with the prompt sliced off.
//
// Still gated behind the checkbox (off by default) and the ADVANCED_OCR_OFFERED
// setting: a fluent model that misreads a claim number is worse than tesseract
// garble, because garble is visibly garble. Measure it on real denial scans
// before offering it.

// eslint-disable-next-line @typescript-eslint/no-require-imports

const QWEN_VL_MODEL_ID = "onnx-community/Qwen3.5-0.8B-ONNX";
// The library's own ES module build, copied from node_modules at build time.
const TRANSFORMERS_MODULE_URL = "/static/js/dist/vendor/transformers/transformers.min.js";
// The ONNX runtime the library drives. Left to itself the library fetches it
// from a public CDN on every cold load: executable code and a 27 MB wasm from
// a third party. The build ships the copy the library was built against
// (webpack.config.js) and the library is pointed at it. One build only, the
// asyncify one: it is the build with WebGPU support, which the model asks
// for. The library's own default swaps in the plain build on Safari, and
// that build cannot do WebGPU, so Safari is not offered the option at all
// (review) until it has been tried there. Published as .js because the web
// image's nginx has no MIME type for .mjs.
const ORT_RUNTIME_URL = "/static/js/dist/vendor/onnxruntime-web/";
const ORT_RUNTIME_BUILD = "ort-wasm-simd-threaded.asyncify";

interface OrtRuntimePaths {
  mjs: string;
  wasm: string;
}

function ortRuntimePaths(): OrtRuntimePaths {
  return { mjs: `${ORT_RUNTIME_URL}${ORT_RUNTIME_BUILD}.js`, wasm: `${ORT_RUNTIME_URL}${ORT_RUNTIME_BUILD}.wasm` };
}

function isSafari(): boolean {
  return typeof navigator !== "undefined" && /^((?!chrome|android).)*safari/i.test(navigator.userAgent);
}
// What we ask of the model. Transcription, not description, in reading order,
// keeping the line breaks a letter has.
const OCR_PROMPT =
  "Transcribe all of the text in this image exactly as written, in reading order, " +
  "keeping the line breaks. Output only the transcribed text. Do not describe the image.";
// Longest side after resize. The vision encoder's cost grows with the number of
// 14 px patches; a 300 DPI letter page is tens of thousands of them, past what
// WebGPU will bind, while 1024 px keeps body text legible at ~1.3k image tokens.
const MAX_IMAGE_SIDE = 1024;
// A dense letter page is a few hundred tokens; the cap bounds a runaway.
// Room for a dense page. A generation that USES all of it did not reach the
// end of the page: it is treated as no reading, and the standard text stays
// (review: a capped transcription used to replace a complete one).
const MAX_NEW_TOKENS = 2048;

// A download with no progress for this long is given up. A route change
// mid-download (the LAN's IPv6 router flapping, a phone switching networks)
// stalls the library's stream without ever rejecting its load, which then
// waits forever (page check: two cold loads in a row hung this way). Only
// counted while a file is actually in flight, so the silence of shader
// compilation after the download does not trip it. Not retried: the
// library dedupes in-flight downloads, so a second load would wait on the
// same stalled stream (review); the person reloads the page instead, and
// the files already downloaded are in the cache for it.
const DOWNLOAD_STALL_MS = 60_000;

class DownloadStalled extends Error {}

interface LoadProgress {
  status: string;
  file?: string;
}

export interface WebGpuAvailability {
  available: boolean;
  reason?: string;
  // Whether the adapter can run half-precision shaders. Decides the vision
  // encoder's precision: many adapters (and every software fallback) lack
  // it, and asking for fp16 there fails the whole load.
  f16?: boolean;
}

interface QwenImage {
  width: number;
  height: number;
  resize(width: number, height: number): Promise<QwenImage> | QwenImage;
}

interface QwenTensor {
  dims: number[];
  slice(...ranges: unknown[]): QwenTensor;
}

type QwenInputs = { input_ids: QwenTensor } & Record<string, unknown>;

interface QwenProcessor {
  (text: string, image: QwenImage): Promise<QwenInputs>;
  apply_chat_template(conversation: unknown, options: Record<string, unknown>): string;
  batch_decode(tokens: QwenTensor, options: { skip_special_tokens: boolean }): string[];
}

interface QwenModel {
  generate(args: Record<string, unknown>): Promise<QwenTensor>;
  dispose?: () => Promise<unknown>;
}

// The library's stop switch for a running generation: interrupt() ends it
// at the next token.
interface StoppingSwitch {
  interrupt(): void;
  reset(): void;
}

interface TransformersModule {
  env: {
    allowLocalModels: boolean;
    backends: { onnx: { wasm: { wasmPaths?: string | OrtRuntimePaths } } };
  };
  AutoProcessor: { from_pretrained(id: string, options: Record<string, unknown>): Promise<QwenProcessor> };
  InterruptableStoppingCriteria: new () => StoppingSwitch;
  Qwen3_5ForConditionalGeneration: {
    from_pretrained(id: string, options: Record<string, unknown>): Promise<QwenModel>;
  };
  RawImage: { read(input: Blob | File | string): Promise<QwenImage> };
}

interface QwenOCRRuntime {
  processor: QwenProcessor;
  model: QwenModel;
  RawImage: TransformersModule["RawImage"];
  StoppingSwitch: TransformersModule["InterruptableStoppingCriteria"];
}

export async function detectWebGPUAvailability(): Promise<WebGpuAvailability> {
  try {
    if (typeof navigator === "undefined" || !("gpu" in navigator)) {
      return { available: false, reason: "navigator.gpu unavailable" };
    }
    if (isSafari()) {
      return { available: false, reason: "Safari: the shipped runtime build has not been tried there" };
    }

    const gpuNavigator = navigator as Navigator & {
      gpu?: {
        requestAdapter: () => Promise<{ features?: { has(name: string): boolean } } | null>;
      };
    };

    const adapter = await gpuNavigator.gpu?.requestAdapter();
    if (!adapter) {
      return { available: false, reason: "No WebGPU adapter" };
    }

    return { available: true, f16: adapter.features?.has("shader-f16") === true };
  } catch (error) {
    const reason = error instanceof Error ? error.message : String(error);
    return { available: false, reason: `WebGPU detection error: ${reason}` };
  }
}

async function loadQwenOCRRuntimeRaw(): Promise<QwenOCRRuntime | null> {
  const webGpu = await detectWebGPUAvailability();
  if (!webGpu.available) {
    console.warn(`[QwenOCR] Disabled: ${webGpu.reason}`);
    return null;
  }

  try {
    // Loaded as a real ES module from our own static files, not bundled by
    // webpack: the ONNX runtime inside it locates its worker and wasm from
    // import.meta.url, which webpack rewrites to a file path and the browser
    // cannot use (the load then fails with "Invalid URL"). A module URL on
    // this origin keeps it correct, and keeps the library first-party.
    const transformers = (await import(
      /* webpackIgnore: true */ TRANSFORMERS_MODULE_URL
    )) as TransformersModule;
    transformers.env.allowLocalModels = false;

    transformers.env.backends.onnx.wasm.wasmPaths = ortRuntimePaths();

    // The dtypes the model card recommends for WebGPU: 4-bit weights for the
    // language model and its embeddings, half precision for the vision
    // encoder where the adapter can run it, full precision otherwise (the
    // load fails outright on an adapter without shader-f16).
    const visionDtype = webGpu.f16 ? "fp16" : "fp32";
    const loadStartedAt = performance.now();
    console.warn(`[QwenOCR] loading (vision encoder ${visionDtype})`);
    // Which files are in flight and when the last byte arrived, for the
    // stall watchdog below.
    const inFlight = new Set<string>();
    let lastProgressAt = performance.now();
    // A file counts as in flight once its transfer has started ("download",
    // then "progress" events) and until "done". Not from "initiate": an
    // optional file the hub answers with a 404 reports "initiate" and never
    // "done", and would have kept the watchdog armed through shader
    // compilation (review). A stall before the first byte is left to the
    // page's own budget.
    const progress_callback = (event: LoadProgress): void => {
      lastProgressAt = performance.now();
      if (!event.file) {
        return;
      }
      if (event.status === "done") {
        inFlight.delete(event.file);
      } else if (event.status === "download" || event.status === "progress") {
        inFlight.add(event.file);
      }
    };
    // Both loads are awaited to the end whichever fails: a rejection that
    // left the other download running would let it refill the cache after
    // the pass had been counted as finished (review).
    // The page can have given up while the import or the adapter detection
    // above was pending; the downloads must not start after that, outside
    // the lock and possibly after a removal (review).
    if (qwenRuntimeDisabled) {
      return null;
    }
    const modelPromise = transformers.Qwen3_5ForConditionalGeneration.from_pretrained(QWEN_VL_MODEL_ID, {
      dtype: { embed_tokens: "q4", vision_encoder: visionDtype, decoder_model_merged: "q4" },
      device: "webgpu",
      progress_callback,
    });
    // The model is held from the moment ITS promise resolves, not from the
    // pair's: a processor that fails or hangs must not strand a model that
    // loaded (review). If the page has given up by then, it is released at
    // once.
    void modelPromise.then(
      (loaded) => {
        loadedModel = loaded;
        if (modelUnwanted) {
          releaseModelIfUnused();
        }
      },
      () => undefined,
    );
    loadPending = true;
    const loads = Promise.allSettled([
      transformers.AutoProcessor.from_pretrained(QWEN_VL_MODEL_ID, { progress_callback }),
      modelPromise,
    ]);
    // Settled, however late. Only a load that fully SUCCEEDED has nothing
    // left running, so only then is the caution this tab raised for it
    // withdrawn: the library gives up on the first failed file but its
    // sibling downloads run on (review).
    void loads.then(([processorSettled, modelSettled]) => {
      loadPending = false;
      if (processorSettled.status === "fulfilled" && modelSettled.status === "fulfilled") {
        clearAbandonedLoad();
      }
    });
    const stalled = new Promise<never>((_, reject) => {
      const timer = window.setInterval(() => {
        if (inFlight.size > 0 && performance.now() - lastProgressAt > DOWNLOAD_STALL_MS) {
          window.clearInterval(timer);
          noteAbandonedLoad();
          reject(new DownloadStalled(`no download progress for ${DOWNLOAD_STALL_MS} ms`));
        }
      }, 5_000);
      void loads.then(() => window.clearInterval(timer));
    });
    const [processorLoad, modelLoad] = await Promise.race([loads, stalled]);
    // Whichever half failed, a model that did load is not kept (review).
    if (processorLoad.status === "rejected") {
      releaseModelIfUnused();
      throw processorLoad.reason;
    }
    if (modelLoad.status === "rejected") {
      releaseModelIfUnused();
      throw modelLoad.reason;
    }
    const processor = processorLoad.value;
    const model = modelLoad.value;
    console.warn(`[QwenOCR] ready in ${Math.round(performance.now() - loadStartedAt)} ms`);
    return { processor, model, RawImage: transformers.RawImage, StoppingSwitch: transformers.InterruptableStoppingCriteria };
  } catch (error) {
    loadFailed = true;
    // Either way the library's own sibling downloads can still be running
    // (it gives up on the first failure, they do not), so every tab's
    // remove control is told, and a model that loaded or lands later is
    // not kept (review).
    noteAbandonedLoad();
    releaseModelIfUnused();
    if (error instanceof DownloadStalled) {
      console.warn("[QwenOCR] download stalled; off until the page is reloaded", error);
    } else {
      console.warn("[QwenOCR] Disabled after initialization failure", error);
    }
    return null;
  }
}

// One load per page, shared by every read. A failed load, stalled or
// otherwise, resolved to null and the model is off for this visit.
let runtimeLoad: Promise<QwenOCRRuntime | null> | null = null;

function loadQwenOCRRuntime(): Promise<QwenOCRRuntime | null> {
  if (!runtimeLoad) {
    runtimeLoad = loadQwenOCRRuntimeRaw();
  }
  return runtimeLoad;
}

let qwenRuntimeDisabled = false;
let loadFailed = false;
// The model once its own load has resolved, whether or not the runtime as
// a whole was used; set unwanted, it is disposed as soon as no generation
// runs on it.
let loadedModel: QwenModel | null = null;
let modelUnwanted = false;
let loadPending = false;

function releaseModelIfUnused(): void {
  modelUnwanted = true;
  const model = loadedModel;
  if (!model) {
    return;
  }
  loadedModel = null;
  void generationSettled.then(() => model.dispose?.());
}

// An abandoned load (stalled, or given up while still under way) can keep
// downloading and put files in the shared cache bucket later, from THIS tab
// or another (review); the remove control in any tab reads this to say so.
// Each tab records its own abandoned load under ITS OWN storage key (a
// shared map would let two tabs' read-modify-writes erase each other,
// review) and clears it when that load has fully succeeded, so nothing of
// it is left running, or the tab goes away; a load that settled by failing
// keeps the caution, since the library's sibling downloads run on after
// its first failure. A tab that crashed without clearing is aged out after
// a day.
const ABANDONED_LOAD_KEY_PREFIX = "fhi_on_device_model_abandoned_load:";
const ABANDONED_LOAD_STALE_MS = 24 * 60 * 60_000;
const tabId = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
const ownAbandonedLoadKey = ABANDONED_LOAD_KEY_PREFIX + tabId;

// This tab's own record of the same thing, for when storage is full or
// blocked (review: the storage write could fail and leave no record).
let abandonedInThisTab = false;

function noteAbandonedLoad(): void {
  abandonedInThisTab = true;
  try {
    window.localStorage.setItem(ownAbandonedLoadKey, String(Date.now()));
  } catch {
    // Storage blocked: this tab's own flag still applies.
  }
}

function clearAbandonedLoad(): void {
  abandonedInThisTab = false;
  try {
    window.localStorage.removeItem(ownAbandonedLoadKey);
  } catch {
    // Nothing to clear where nothing could be written.
  }
}

if (typeof window !== "undefined") {
  // A page put into the back/forward cache is not gone: it can come back
  // and its downloads with it, so its record stays (review).
  window.addEventListener("pagehide", (event: PageTransitionEvent) => {
    if (!event.persisted) {
      clearAbandonedLoad();
    }
  });
}

export function onDeviceLoadMayStillBeRunning(): boolean {
  if (abandonedInThisTab) {
    return true;
  }
  const now = Date.now();
  try {
    const storage = window.localStorage;
    for (let i = 0; i < storage.length; i += 1) {
      const key = storage.key(i);
      if (key && key.startsWith(ABANDONED_LOAD_KEY_PREFIX)) {
        const at = Number(storage.getItem(key) ?? "0");
        if (at > 0 && now - at < ABANDONED_LOAD_STALE_MS) {
          return true;
        }
      }
    }
  } catch {
    // Storage blocked: only this tab's own state above is known.
  }
  return false;
}
// The stop switch of the generation running right now, if any, and a
// promise that settles when it has actually stopped: the library checks the
// switch only between forward passes, so stopping takes a moment.
let runningGeneration: StoppingSwitch | null = null;
let generationSettled: Promise<void> = Promise.resolve();

// Settles once no generation is running. Callers that give up wait on it
// before treating the GPU as free.
export function onDeviceGenerationSettled(): Promise<void> {
  return generationSettled;
}


// Whether a load of the model failed on this page. The library fetches a
// model's files together and gives up on the first failure, but the
// others keep downloading and land in its cache when they finish; the
// remove control says so instead of promising a clean removal (review).
export function onDeviceModelLoadFailed(): boolean {
  return loadFailed;
}

// Called when a read ran past its time budget. A download interrupted by a
// network change left the library's load pending forever (page check:
// ERR_NETWORK_CHANGED mid-download, the pass never finished); every later
// read would wait on that same memoized load, so the model is switched off
// for the rest of this page.
export function giveUpOnDeviceModel(): void {
  loadFailed = true;
  qwenRuntimeDisabled = true;
  // A generation still running is stopped at its next token, so the GPU is
  // free for whoever takes the lock next (review: it used to run on after
  // the pass had given up and released the lock).
  runningGeneration?.interrupt();
  // A load still under way can keep downloading into the shared bucket;
  // every tab's remove control says so for a while.
  if (loadPending) {
    noteAbandonedLoad();
  }
  // The model, loaded or still loading, is not wanted any more; it is
  // released once no generation runs on it (review).
  releaseModelIfUnused();
}

export async function recognizeWithQwenWebGPU(input: Blob | File | string): Promise<string> {
  if (qwenRuntimeDisabled) {
    return "";
  }

  const runtime = await loadQwenOCRRuntime();
  if (!runtime) {
    return "";
  }
  // Again after the load: a load that outran its page's budget still
  // completes, and must not go on to a generation nobody is waiting for
  // (review).
  if (qwenRuntimeDisabled) {
    return "";
  }

  const startedAt = performance.now();
  try {
    let image = await runtime.RawImage.read(input);
    const scale = MAX_IMAGE_SIDE / Math.max(image.width, image.height);
    if (scale < 1) {
      image = await image.resize(
        Math.round(image.width * scale),
        Math.round(image.height * scale),
      );
    }
    const conversation = [
      { role: "user", content: [{ type: "image" }, { type: "text", text: OCR_PROMPT }] },
    ];
    // enable_thinking: false keeps the model from reasoning aloud before it
    // transcribes; templates without that switch ignore it.
    const prompt = runtime.processor.apply_chat_template(conversation, {
      add_generation_prompt: true,
      enable_thinking: false,
    });
    const inputs = await runtime.processor(prompt, image);
    // Once more right before the expensive part: the page's budget can run
    // out during the image work above (review).
    if (qwenRuntimeDisabled) {
      return "";
    }
    const stop = new runtime.StoppingSwitch();
    runningGeneration = stop;
    const generation = runtime.model.generate({
      ...inputs,
      max_new_tokens: MAX_NEW_TOKENS,
      do_sample: false,
      stopping_criteria: stop,
    });
    generationSettled = generation.then(
      () => undefined,
      () => undefined,
    );
    let outputs: QwenTensor;
    try {
      outputs = await generation;
    } finally {
      runningGeneration = null;
    }
    // Stopped from outside: whatever came out is not a reading.
    if (qwenRuntimeDisabled) {
      return "";
    }
    // Only the new tokens: everything up to the prompt length is the prompt.
    const promptLength = inputs.input_ids.dims[inputs.input_ids.dims.length - 1];
    const generated = outputs.dims[outputs.dims.length - 1] - promptLength;
    if (generated >= MAX_NEW_TOKENS) {
      console.warn(`[QwenOCR] transcription hit the ${MAX_NEW_TOKENS}-token cap; keeping the standard reading`);
      return "";
    }
    const decoded = runtime.processor.batch_decode(outputs.slice(null, [promptLength, null]), {
      skip_special_tokens: true,
    });
    // A thinking block that slipped through is not transcription.
    const text = (decoded[0] ?? "").replace(/<think>[\s\S]*?<\/think>/g, "").trim();
    // Length and time only, never the text: this is the one line that says
    // the engine ran, and warn is the level the production build keeps.
    console.warn(`[QwenOCR] transcribed ${text.length} chars in ${Math.round(performance.now() - startedAt)} ms`);
    return text;
  } catch (error) {
    console.warn("[QwenOCR] Inference failed; continuing without Qwen OCR", error);
    qwenRuntimeDisabled = true;
    // Off for the visit, so the model's sessions go too (review: they sat
    // in GPU memory until reload).
    releaseModelIfUnused();
    return "";
  }
}
