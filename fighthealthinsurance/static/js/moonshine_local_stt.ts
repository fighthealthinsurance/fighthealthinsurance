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

export const getPreferredBackend = (): "webgpu" | "wasm" => {
  return typeof navigator !== "undefined" && "gpu" in navigator ? "webgpu" : "wasm";
};

export const createMoonshineClient = async (): Promise<LocalSTTClient> => {
  if (!window.MoonshineWeb?.createTranscriber) {
    throw new Error("Moonshine Web is unavailable in this browser build.");
  }
  return window.MoonshineWeb.createTranscriber({ backend: getPreferredBackend() });
};
