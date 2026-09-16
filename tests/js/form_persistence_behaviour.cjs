// Runs the compiled formPersistence bundle against a fake DOM and a fake
// localStorage, and reports what the restore rule did. Driven by
// tests/sync/test_form_persistence_behaviour.py; prints one JSON object.
//
// The bundle initialises itself on DOMContentLoaded; the fake document
// records listeners and this fires that event, so production's own
// initialisation is what runs, not a manual call.
//
// usage: node form_persistence_behaviour.cjs <bundle path> <scenario>
//   plain-get:        no fingerprint on the page
//   server-spoke:     the page carries <input name="health_history_seen">
//   server-spoke-two-keys: same, with a session scope so the browser holds a
//                     scoped copy holding "" and a bare copy holding text
const fs = require('fs');
const vm = require('vm');

const [bundlePath, scenario] = process.argv.slice(2);
const code = fs.readFileSync(bundlePath, 'utf8');
const sessionScoped = scenario === 'server-spoke-two-keys';
const serverSpoke = scenario.startsWith('server-spoke');

const store = new Map();
const localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};

function element(tag, attrs) {
  const listeners = {};
  return Object.assign({
    tagName: tag,
    value: '',
    dataset: {},
    addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
    _fire: (type) => (listeners[type] || []).forEach((fn) => fn({})),
    getAttribute: (n) => (attrs && n in attrs ? attrs[n] : null),
  }, attrs || {});
}

const form = element('form', {});
const textarea = element('textarea', { id: 'health_history', form });
const seenInput = serverSpoke ? element('input', { name: 'health_history_seen', value: 'abc' }) : null;
const sessionMeta = sessionScoped ? element('meta', { name: 'fhi-session-key', content: 'sess1' }) : null;

const byId = { health_history: textarea };
const docListeners = {};
const document = {
  body: element('body', {}),
  cookie: '',
  readyState: 'loading',
  getElementById: (id) => byId[id] || null,
  querySelector: (sel) => {
    if (sel === 'input[name="health_history_seen"]') return seenInput;
    if (sel === 'meta[name="fhi-session-key"]') return sessionMeta;
    return null;
  },
  querySelectorAll: () => [],
  addEventListener: (type, fn) => { (docListeners[type] = docListeners[type] || []).push(fn); },
  referrer: '',
};
const window = {
  localStorage,
  document,
  addEventListener: () => {},
  location: { pathname: '/hh', search: '', href: 'http://t/hh' },
  performance: { getEntriesByType: () => [] },
};
const sandbox = { window, document, localStorage, self: window, globalThis: window, console, setTimeout, clearTimeout };
window.window = window;
vm.runInNewContext(code, sandbox);

// What an earlier visit left behind, written through the bundle's own setter
// so the wrapper format and the key scoping are its own.
const api = window.formPersistence;
const stamp = JSON.stringify({ value: 'old text from this browser', expiry: Date.now() + 60000 });
if (sessionScoped) {
  // The scoped key holds "" (a cleared box that never submitted), the bare
  // key from before the session scope existed holds the text.
  localStorage.setItem('fhi_session_sess1_health_history', JSON.stringify({ value: '', expiry: Date.now() + 60000 }));
  localStorage.setItem('health_history', stamp);
} else {
  api.setLocalStorageItemWithTTL('health_history', 'old text from this browser');
}
const keysBefore = [...store.keys()];

// Production's own initialisation.
(docListeners['DOMContentLoaded'] || []).forEach((fn) => fn({}));

process.stdout.write(JSON.stringify({
  scenario,
  initialised: (docListeners['DOMContentLoaded'] || []).length > 0,
  keysBefore,
  boxAfter: textarea.value,
  keysAfter: [...store.keys()],
  textLeftAnywhere: [...store.values()].some((v) => v.includes('old text from this browser')),
}) + '\n');
