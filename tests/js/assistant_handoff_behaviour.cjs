'use strict';
// The browser side of the assistant handoff, run rather than read. Driven by
// tests/sync/test_assistant_handoff.py; prints one JSON object.
//
//   node assistant_handoff_behaviour.cjs landing <script file> <scenario>
//     Runs the landing page's first <script> (taken from the rendered page)
//     against a fake address bar and the fake page from fake_page.cjs, then
//     fires DOMContentLoaded and a submit, as a browser would.
//     Scenarios: with-code, no-code, bad-code.
//
//   node assistant_handoff_behaviour.cjs keep <compiled dir> <scenario>
//     Loads shared.ts and scrub_client_side_form.ts as compiled into <dir>,
//     calls keepServerFilledText on a letter box the server filled in, fires
//     DOMContentLoaded (where scrub_client_side_form restores stored
//     values), then reads the box back the way a later /scan does.
//     Scenarios: from-assistant, remember-off, not-from-assistant.
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const {buildPage} = require(path.join(__dirname, 'fake_page.cjs'));

const [, , mode, file, scenario] = process.argv;
if (!mode || !file || !scenario) {
  throw new Error('usage: assistant_handoff_behaviour.cjs <landing|keep> <file> <scenario>');
}

// The ids the landing script uses; the Python test checks them against the
// rendered page, so a rename cannot leave this fixture testing nothing.
const landingMarkup = (bind) => `
<div id="handoff-ready">
  <form id="handoff-form" action="/from-your-assistant"${bind ? ' data-bind="1"' : ''}>
    <input name="csrfmiddlewaretoken" value="csrf-token" />
    <input id="handoff-token" name="token" value="" />
    <button id="handoff-open" type="submit">Open my appeal form</button>
  </form>
</div>
<div id="handoff-dead"></div>
`;

async function landing() {
  const script = fs.readFileSync(file, 'utf8');
  const code = crypto.randomBytes(32).toString('base64url');
  const hash = {
    'with-code': '#' + code,
    'no-code': '',
    'bad-code': '#not-a-code',
    'with-bind': '#' + code,
    'bind-refused': '#' + code,
  }[scenario];
  if (hash === undefined) throw new Error('unknown scenario ' + scenario);
  const binds = scenario === 'with-bind' || scenario === 'bind-refused';

  const page = buildPage(landingMarkup(binds));
  const $ = (id) => page.document.getElementById(id);
  // As the server renders them: the dead block hidden, the button off.
  $('handoff-dead').hidden = true;
  $('handoff-ready').hidden = false;
  $('handoff-open').disabled = true;
  $('handoff-token').value = '';
  // Inputs built from markup carry value= only as an attribute.
  const csrf = page.document.querySelector('input[name="csrfmiddlewaretoken"]');
  if (csrf) csrf.value = csrf.getAttribute('value');

  const replaced = [];
  const location = {pathname: '/from-your-assistant', search: '', hash};
  const history = {
    replaceState: (state, title, url) => {
      replaced.push(String(url));
      const at = String(url).indexOf('#');
      location.hash = at >= 0 ? String(url).slice(at) : '';
    },
  };
  const window = {location, history};
  // A fake fetch that records the bind request and answers as the server would.
  const fetched = [];
  const fetch = (url, init) => {
    fetched.push({url, method: init.method, body: init.body, credentials: init.credentials});
    const bound = scenario === 'with-bind';
    return Promise.resolve({json: () => Promise.resolve({bound})});
  };
  const sandbox = {window, document: page.document, console, fetch, encodeURIComponent};
  vm.runInNewContext(script, sandbox);
  // Straight after the script, before the page has even finished parsing.
  const hashAfterScript = location.hash;

  page.fireDomReady();
  // Let the bind request's promise settle, as a browser would before any click.
  await new Promise((resolve) => setImmediate(resolve));
  const afterReady = {
    token: $('handoff-token').value,
    buttonDisabled: $('handoff-open').disabled,
    readyHidden: $('handoff-ready').hidden,
    deadHidden: $('handoff-dead').hidden,
  };
  $('handoff-form').dispatch('submit', {});
  return {
    scenario,
    codeLength: code.length,
    codeMatchesToken: afterReady.token === code,
    hashAfterScript,
    replaced,
    ...afterReady,
    buttonDisabledAfterSubmit: $('handoff-open').disabled,
    fetched,
    // Nothing the script keeps leaks onto the page's globals.
    globalsAdded: Object.keys(sandbox).filter(
      (k) => !['window', 'document', 'console', 'fetch', 'encodeURIComponent'].includes(k)
    ),
  };
}

function keep() {
  const store = new Map();
  const localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    key: (i) => [...store.keys()][i] || null,
    get length() {
      return store.size;
    },
  };
  const letter = 'Your claim for an MRI of the lower back was denied.';
  const box = {
    id: 'denial_text',
    value: letter,
    getAttribute: (name) =>
      name === 'data-from-assistant' && scenario !== 'not-from-assistant' ? 'true' : null,
  };
  const ready = [];
  global.window = {localStorage};
  global.document = {
    cookie: '',
    getElementById: (id) => (id === 'denial_text' ? box : null),
    querySelectorAll: () => [],
    addEventListener: (type, fn) => {
      if (type === 'DOMContentLoaded') ready.push(fn);
    },
  };
  // shared.ts sets pdf.js's worker path on import; nothing here reads a PDF.
  const Module = require('module');
  const load = Module._load;
  Module._load = function (request, ...rest) {
    if (request === 'pdfjs-dist') return {GlobalWorkerOptions: {}};
    return load.call(this, request, ...rest);
  };
  const shared = require(path.join(path.resolve(file), 'shared.js'));
  // Registers its DOMContentLoaded restore, as the page's bundle does.
  require(path.join(path.resolve(file), 'scrub_client_side_form.js'));

  if (scenario === 'remember-off') {
    // "Remember what I typed" unticked, the way the page's checkbox does it.
    shared.setPersistenceEnabled(false);
  } else if (!['from-assistant', 'not-from-assistant'].includes(scenario)) {
    throw new Error('unknown scenario ' + scenario);
  }
  const saved = shared.keepServerFilledText(box);
  ready.forEach((fn) => fn({}));
  return {
    scenario,
    saved,
    loadedHandlers: ready.length,
    // The box once the page has finished loading.
    boxAfterLoad: box.value,
    // What a later /scan puts back into its empty box.
    restored: shared.getLocalStorageItemWithTTL('denial_text'),
    letter,
    keys: [...store.keys()],
  };
}

const result = mode === 'landing' ? landing() : mode === 'keep' ? Promise.resolve(keep()) : null;
if (result === null) throw new Error('unknown mode ' + mode);
result.then((value) => process.stdout.write(JSON.stringify(value) + '\n'));
