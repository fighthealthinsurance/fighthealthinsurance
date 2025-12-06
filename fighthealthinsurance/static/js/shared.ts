import * as pdfjsLib from "pdfjs-dist";

// TTL for localStorage persistence (best-effort expiration)
const LOCAL_STORAGE_TTL_MS = 24 * 60 * 60 * 1000;

// Key for the persistence preference
const PERSISTENCE_ENABLED_KEY = "fhi_persistence_enabled";

// Interface for stored values with TTL
interface StoredValueWithTTL {
  value: string;
  expiry: number;
}

// Check if persistence is enabled (default: true)
function isPersistenceEnabled(): boolean {
  const stored = window.localStorage.getItem(PERSISTENCE_ENABLED_KEY);
  // Default to true if not set
  return stored !== "false";
}

// Set persistence preference
function setPersistenceEnabled(enabled: boolean): void {
  window.localStorage.setItem(PERSISTENCE_ENABLED_KEY, enabled.toString());
  if (!enabled) {
    // Clear all form data when disabling persistence
    clearFormData();
  }
}

// Clear all stored form data (but keep the persistence preference)
function clearFormData(): void {
  const keysToRemove: string[] = [];
  for (let i = 0; i < window.localStorage.length; i++) {
    const key = window.localStorage.key(i);
    if (key && (key.startsWith("store_") || key === "email" || key === "denial_text")) {
      keysToRemove.push(key);
    }
  }
  keysToRemove.forEach(key => window.localStorage.removeItem(key));
}

// Get item with TTL check
function getLocalStorageItemWithTTL(key: string): string | null {

  // If someones disabled persistence remove items if found.
  const stored = window.localStorage.getItem(key);
  if (!isPersistenceEnabled()) {
      window.localStorage.removeItem(key);
      return null;
  }
  if (!stored) {
    return null;
  }

  try {
    const parsed: StoredValueWithTTL = JSON.parse(stored);
    if (parsed.expiry && Date.now() > parsed.expiry) {
      // Item has expired, remove it
      window.localStorage.removeItem(key);
      return null;
    }
    return parsed.value;
  } catch {
    // Backwards compatibility: if it's not JSON, return the raw value
    return stored;
  }
}

// Set item with TTL
function setLocalStorageItemWithTTL(key: string, value: string): void {
  if (!isPersistenceEnabled()) {
    return;
  }

  const item: StoredValueWithTTL = {
    value: value,
    expiry: Date.now() + LOCAL_STORAGE_TTL_MS,
  };
  window.localStorage.setItem(key, JSON.stringify(item));
}

// Get item with TTL check and default value
function getLocalStorageItemOrDefault(
  key: string,
  defaultValue: string,
): string {
  const value = getLocalStorageItemWithTTL(key);
  return value !== null ? value : defaultValue;
}

function getLocalStorageItemOrDefaultEQ(key: string): string {
  return getLocalStorageItemOrDefault(key, key);
}

const storeLocal = async function (event: Event) {
  const target = event.target as HTMLInputElement;
  const name = target.id;
  const value = target.value;
  setLocalStorageItemWithTTL(name, value);
};

// Store textarea content with TTL
const storeTextareaLocal = async function (event: Event) {
  const target = event.target as HTMLTextAreaElement;
  const name = target.id;
  const value = target.value;
  setLocalStorageItemWithTTL(name, value);
};

const node_module_path = "/static/js/node_modules/";

// pdf.js
pdfjsLib.GlobalWorkerOptions.workerSrc =
  node_module_path + "pdfjs-dist/build/pdf.worker.min.mjs";

export {
  storeLocal,
  storeTextareaLocal,
  pdfjsLib,
  node_module_path,
  getLocalStorageItemOrDefault,
  getLocalStorageItemOrDefaultEQ,
  getLocalStorageItemWithTTL,
  setLocalStorageItemWithTTL,
  isPersistenceEnabled,
  setPersistenceEnabled,
  clearFormData,
};
