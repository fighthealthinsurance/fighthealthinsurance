// Lightweight form persistence with TTL (no heavy dependencies)
// Use this module for pages that just need localStorage get/set with TTL

// TTL for localStorage persistence (best-effort expiration)
const LOCAL_STORAGE_TTL_MS = 24 * 60 * 60 * 1000;

// Key for the persistence preference
const PERSISTENCE_ENABLED_KEY = "fhi_persistence_enabled";

// Key prefix for session-scoped persistence
const SESSION_KEY_PREFIX = "fhi_session_";

// Get current session key from the page (set by Django template)
function getSessionKey(): string | null {
  const metaElement = document.querySelector('meta[name="fhi-session-key"]');
  return metaElement ? metaElement.getAttribute('content') : null;
}

// Check if page was rendered via POST (set by Django template)
function isPostResponse(): boolean {
  const metaElement = document.querySelector('meta[name="fhi-request-method"]');
  return metaElement ? metaElement.getAttribute('content') === 'POST' : false;
}

// Check if this is the final PII page that always needs restore
function isAlwaysRestorePage(): boolean {
  const metaElement = document.querySelector('meta[name="fhi-always-restore"]');
  return metaElement ? metaElement.getAttribute('content') === 'true' : false;
}

// Get session-scoped storage key
function getSessionScopedKey(key: string): string {
  const sessionKey = getSessionKey();
  if (sessionKey) {
    return `${SESSION_KEY_PREFIX}${sessionKey}_${key}`;
  }
  // Fallback to global key if no session
  return key;
}

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

// Get item with TTL check (uses session-scoped key if session is available)
export function getLocalStorageItemWithTTL(key: string, useSessionScope: boolean = true): string | null {
  if (!isPersistenceEnabled()) {
    return null;
  }

  const storageKey = useSessionScope ? getSessionScopedKey(key) : key;
  const stored = window.localStorage.getItem(storageKey);
  if (!stored) {
    // Also check non-session-scoped key for backwards compatibility
    if (useSessionScope && getSessionKey()) {
      return getLocalStorageItemWithTTL(key, false);
    }
    return null;
  }

  try {
    const parsed: StoredValueWithTTL = JSON.parse(stored);
    if (parsed.expiry && Date.now() > parsed.expiry) {
      // Item has expired, remove it
      window.localStorage.removeItem(storageKey);
      return null;
    }
    return parsed.value;
  } catch {
    // Backwards compatibility: if it's not JSON, return the raw value
    return stored;
  }
}

// Set item with TTL (uses session-scoped key if session is available)
export function setLocalStorageItemWithTTL(key: string, value: string): void {
  if (!isPersistenceEnabled()) {
    return;
  }

  const storageKey = getSessionScopedKey(key);
  const item: StoredValueWithTTL = {
    value: value,
    expiry: Date.now() + LOCAL_STORAGE_TTL_MS,
  };
  window.localStorage.setItem(storageKey, JSON.stringify(item));
}

// Both keys: a write is session-scoped only while base.html rendered the
// session meta tag, and a read falls back from the scoped key to the bare
// one, so one field can hold two values and a read can return the older.
export function clearLocalStorageItem(key: string): void {
  window.localStorage.removeItem(getSessionScopedKey(key));
  window.localStorage.removeItem(key);
}

// Helper to setup persistence for a textarea element
// Options:
// - alwaysRestore: if true, always restore from localStorage (for PII fill-in page)
export function setupTextareaPersistence(textareaId: string, options?: { alwaysRestore?: boolean }): void {
  const textarea = document.getElementById(textareaId) as HTMLTextAreaElement | null;
  if (textarea) {
    // Determine if we should restore
    // - On POST responses, only restore if the field is empty AND (alwaysRestore OR isAlwaysRestorePage)
    // - On GET responses, always restore if field is empty
    // A page that carries a fingerprint of what the server rendered
    // (<input name="<id>_seen">) has said what is stored, empty included.
    // A local copy from an earlier visit must not be put over that: the
    // history may have been removed from another device since, and the
    // server would read the restored text as the person typing it back.
    const serverSaidWhatIsStored = () =>
      document.querySelector('input[name="' + textareaId + '_seen"]') !== null;

    const shouldRestore = () => {
      if (textarea.value !== '') {
        return false;  // Field already has a value, don't overwrite
      }
      if (serverSaidWhatIsStored()) {
        return false;
      }
      if (options?.alwaysRestore || isAlwaysRestorePage()) {
        return true;  // This page always needs restore (e.g., PII fill-in)
      }
      if (isPostResponse()) {
        return false;  // POST response with empty field - server intentionally left it empty
      }
      return true;  // GET response with empty field - restore from localStorage
    };

    // Restore saved value if conditions are met
    const saved = getLocalStorageItemWithTTL(textareaId);
    if (saved && shouldRestore()) {
      textarea.value = saved;
    } else if (saved && serverSaidWhatIsStored()) {
      // Stale by definition: the server's word replaces it.
      clearLocalStorageItem(textareaId);
    }
    // Save on input
    textarea.addEventListener('input', function() {
      setLocalStorageItemWithTTL(textareaId, textarea.value);
    });
    // An empty box on submit is a deletion, and the next GET renders it back
    // empty -- exactly the state shouldRestore() treats as "restore", so a
    // surviving copy would refill the box the person just cleared.
    // textarea.form resolves through the form="" attribute this page uses.
    const owner = textarea.form;
    if (owner) {
      owner.addEventListener('submit', function() {
        if (textarea.value === '') {
          clearLocalStorageItem(textareaId);
        }
      });
    }
  }
}

// Helper to setup persistence for an input element
// Options:
// - alwaysRestore: if true, always restore from localStorage (for PII fill-in page)
export function setupInputPersistence(inputId: string, options?: { alwaysRestore?: boolean }): void {
  const input = document.getElementById(inputId) as HTMLInputElement | null;
  if (input) {
    // Determine if we should restore (same logic as textarea)
    const shouldRestore = () => {
      if (input.value !== '') {
        return false;
      }
      if (options?.alwaysRestore || isAlwaysRestorePage()) {
        return true;
      }
      if (isPostResponse()) {
        return false;
      }
      return true;
    };

    // Restore saved value if conditions are met
    const saved = getLocalStorageItemWithTTL(inputId);
    if (saved && shouldRestore()) {
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
    clearLocalStorageItem,
    isPersistenceEnabled,
    getSessionKey,
    isPostResponse,
    isAlwaysRestorePage,
  };
}
