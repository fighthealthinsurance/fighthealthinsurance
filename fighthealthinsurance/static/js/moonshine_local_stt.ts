export interface LocalSTTClient {
  transcribe(audioBlob: Blob): Promise<string>;
}

declare global {
  interface Window {
    MoonshineWeb?: {
      createTranscriber: (opts: { backend: "webgpu" | "wasm" }) => Promise<LocalSTTClient>;
    };
  }
}

export const getPreferredBackend = async (): Promise<"webgpu" | "wasm"> => {
  if (typeof navigator === "undefined" || !("gpu" in navigator)) {
    return "wasm";
  }

  try {
    const adapter = await (navigator as Navigator & { gpu?: { requestAdapter: () => Promise<unknown> } }).gpu?.requestAdapter();
    return adapter ? "webgpu" : "wasm";
  } catch {
    return "wasm";
  }
};

export const createMoonshineClient = async (): Promise<LocalSTTClient> => {
  if (!window.MoonshineWeb?.createTranscriber) {
    throw new Error("Moonshine Web is unavailable in this browser build.");
  }

  const preferredBackend = await getPreferredBackend();
  try {
    return await window.MoonshineWeb.createTranscriber({ backend: preferredBackend });
  } catch (error) {
    if (preferredBackend === "webgpu") {
      return window.MoonshineWeb.createTranscriber({ backend: "wasm" });
    }
    throw error;
  }
};
