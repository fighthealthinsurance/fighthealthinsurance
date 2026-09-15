// Runs the compiled formPersistence bundle against a fake DOM and a fake
// localStorage, and reports what the restore rule did. Driven by
// tests/sync/test_form_persistence_behaviour.py; prints one JSON object.
//
// usage: node form_persistence_behaviour.cjs <bundle path> <scenario>
//   scenario "server-spoke": the page carries <input name="health_history_seen">
//   scenario "plain-get":    it does not
const fs = require('fs');
const vm = require('vm');

const [bundlePath, scenario] = process.argv.slice(2);
const code = fs.readFileSync(bundlePath, 'utf8');

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
const seenInput = scenario === 'server-spoke'
  ? element('input', { name: 'health_history_seen', value: 'abc' })
  : null;

const byId = { health_history: textarea };
const document = {
  body: element('body', {}),
  cookie: '',
  readyState: 'complete',
  getElementById: (id) => byId[id] || null,
  querySelector: (sel) => (sel === 'input[name="health_history_seen"]' ? seenInput : null),
  querySelectorAll: () => [],
  addEventListener: () => {},
  referrer: '',
};
const window = {
  localStorage,
  document,
  addEventListener: () => {},
  location: { pathname: '/hh', search: '', href: 'http://t/hh' },
  performance: { getEntriesByType: () => [] },
  navigation: undefined,
};
const sandbox = { window, document, localStorage, self: window, globalThis: window, console, setTimeout, clearTimeout };
window.window = window;
vm.runInNewContext(code, sandbox);

const api = window.formPersistence;
// An earlier visit left a copy in this browser.
api.setLocalStorageItemWithTTL('health_history', 'old text from this browser');
const before = api.getLocalStorageItemWithTTL('health_history');

api.setupTextareaPersistence('health_history');

process.stdout.write(JSON.stringify({
  scenario,
  hadCopy: before === 'old text from this browser',
  boxAfter: textarea.value,
  copyAfter: api.getLocalStorageItemWithTTL('health_history'),
}) + '\n');
