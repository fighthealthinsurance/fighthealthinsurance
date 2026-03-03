// Optional Qwen WebGPU OCR path.
// This module must fail closed: if WebGPU/model init fails, callers keep using baseline OCR.

// eslint-disable-next-line @typescript-eslint/no-require-imports
const memoizeOne = require("async-memoize-one");

const QWEN_VL_MODEL_ID = "Qwen/Qwen3.5-VL-4B-Instruct";

export interface WebGpuAvailability {
  available: boolean;
  reason?: string;
}

interface GeneratedTextResult {
  generated_text?: string;
  text?: string;
}

type ImageToTextCallable = (
  input: Blob | File | string,
  options: { max_new_tokens: number }
) => Promise<GeneratedTextResult[] | GeneratedTextResult>;

interface TransformersModule {
  env: { allowLocalModels: boolean };
  pipeline: (
    task: string,
    model: string,
    options: { device: string; dtype: string }
  ) => Promise<unknown>;
}

function getGeneratedText(result: GeneratedTextResult[] | GeneratedTextResult): string {
  if (Array.isArray(result)) {
    return result
      .map((entry) => entry.generated_text ?? entry.text ?? "")
      .join("\n")
      .trim();
  }

  return (result.generated_text ?? result.text ?? "").trim();
}

async function detectWebGPUAvailability(): Promise<WebGpuAvailability> {
  if (typeof navigator === "undefined" || !("gpu" in navigator)) {
    return { available: false, reason: "navigator.gpu unavailable" };
  }

  const gpuNavigator = navigator as Navigator & {
    gpu?: { requestAdapter: () => Promise<unknown | null> };
  };

  const adapter = await gpuNavigator.gpu?.requestAdapter();
  if (!adapter) {
    return { available: false, reason: "No WebGPU adapter" };
  }

  return { available: true };
}

async function loadQwenOCRPipelineRaw(): Promise<ImageToTextCallable | null> {
  const webGpu = await detectWebGPUAvailability();
  if (!webGpu.available) {
    console.warn(`[QwenOCR] Disabled: ${webGpu.reason}`);
    return null;
  }

  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const transformers = require("@huggingface/transformers") as TransformersModule;
    transformers.env.allowLocalModels = false;

    const pipe = (await transformers.pipeline("image-to-text", QWEN_VL_MODEL_ID, {
      device: "webgpu",
      dtype: "q4",
    })) as ImageToTextCallable;

    return pipe;
  } catch (error) {
    console.warn("[QwenOCR] Disabled after initialization failure", error);
    return null;
  }
}

const loadQwenOCRPipeline = memoizeOne(loadQwenOCRPipelineRaw);
let qwenRuntimeDisabled = false;

export async function recognizeWithQwenWebGPU(input: Blob | File | string): Promise<string> {
  if (qwenRuntimeDisabled) {
    return "";
  }

  const pipeline = await loadQwenOCRPipeline();
  if (!pipeline) {
    return "";
  }

  try {
    const result = await pipeline(input, { max_new_tokens: 768 });
    return getGeneratedText(result);
  } catch (error) {
    console.warn("[QwenOCR] Inference failed; continuing without Qwen OCR", error);
    qwenRuntimeDisabled = true;
    return "";
  }
}
