// Lightweight form persistence with TTL (no heavy dependencies)
// Use this module for pages that just need localStorage get/set with TTL

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
export function isPersistenceEnabled(): boolean {
  const stored = window.localStorage.getItem(PERSISTENCE_ENABLED_KEY);
  // Default to true if not set
  return stored !== "false";
}

// Get item with TTL check
export function getLocalStorageItemWithTTL(key: string): string | null {
  if (!isPersistenceEnabled()) {
    return null;
  }

  const stored = window.localStorage.getItem(key);
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
export function setLocalStorageItemWithTTL(key: string, value: string): void {
  if (!isPersistenceEnabled()) {
    return;
  }

  const item: StoredValueWithTTL = {
    value: value,
    expiry: Date.now() + LOCAL_STORAGE_TTL_MS,
  };
  window.localStorage.setItem(key, JSON.stringify(item));
}

// Helper to setup persistence for a textarea element
export function setupTextareaPersistence(textareaId: string): void {
  const textarea = document.getElementById(textareaId) as HTMLTextAreaElement | null;
  if (textarea) {
    // Restore saved value if field is empty
    const saved = getLocalStorageItemWithTTL(textareaId);
    if (saved && textarea.value === '') {
      textarea.value = saved;
    }
    // Save on input
    textarea.addEventListener('input', function() {
      setLocalStorageItemWithTTL(textareaId, textarea.value);
    });
  }
}

// Helper to setup persistence for an input element
export function setupInputPersistence(inputId: string): void {
  const input = document.getElementById(inputId) as HTMLInputElement | null;
  if (input) {
    // Restore saved value if field is empty
    const saved = getLocalStorageItemWithTTL(inputId);
    if (saved && input.value === '') {
      input.value = saved;
    }
    // Save on change
    input.addEventListener('change', function() {
      setLocalStorageItemWithTTL(inputId, input.value);
    });
  }
}

// Auto-initialize for known form elements when DOM is ready
if (typeof document !== 'undefined') {
  document.addEventListener('DOMContentLoaded', function() {
    // Health history page
    const healthHistory = document.getElementById('health_history');
    if (healthHistory) {
      setupTextareaPersistence('health_history');
    }
  });
}

// Export to window for manual use if needed
if (typeof window !== 'undefined') {
  (window as any).formPersistence = {
    setupTextareaPersistence,
    setupInputPersistence,
    getLocalStorageItemWithTTL,
    setLocalStorageItemWithTTL,
    isPersistenceEnabled,
  };
}
