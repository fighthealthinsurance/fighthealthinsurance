'use strict';
// A DOM small enough to read in one sitting and real enough to answer the one
// question the source-text tests in test_entity_extract_frames.py cannot: what
// does the extraction page actually put in front of the person when a run
// ends, and does anything move them off it?
//
// There is no jsdom in this repo and node_modules is gitignored, so the shim
// is hand written. It covers exactly what entity_fetcher.ts touches. Anything
// the page reaches for that is not here throws rather than returning
// undefined, so a rewrite that starts using a new DOM API fails loudly instead
// of quietly doing nothing under the test.

function camel(name) {
  return name.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
}

class Style {
  set cssText(value) {
    for (const rule of String(value).split(';')) {
      const at = rule.indexOf(':');
      if (at < 0) continue;
      const prop = camel(rule.slice(0, at).trim());
      if (!prop) continue;
      this[prop] = rule.slice(at + 1).trim();
    }
  }
  get cssText() {
    return Object.keys(this)
      .map((k) => k + ': ' + this[k])
      .join('; ');
  }
}

class TextNode {
  constructor(data) {
    this.nodeType = 3;
    this.data = data;
    this.parentNode = null;
  }
  get textContent() {
    return this.data;
  }
}

class FakeElement {
  constructor(tag, page) {
    this.nodeType = 1;
    this.tagName = String(tag).toUpperCase();
    this.page = page;
    this.childNodes = [];
    this.parentNode = null;
    this.attributes = {};
    this.style = new Style();
    this.id = '';
    this.className = '';
    this.listeners = {};
  }

  setAttribute(name, value) {
    if (name === 'id') this.id = value;
    else if (name === 'class') this.className = value;
    else if (name === 'style') this.style.cssText = value;
    else this.attributes[name] = value;
  }

  getAttribute(name) {
    if (name === 'id') return this.id;
    if (name === 'class') return this.className;
    return this.attributes[name];
  }

  appendChild(node) {
    if (node.parentNode) node.parentNode.removeChild(node);
    node.parentNode = this;
    this.childNodes.push(node);
    return node;
  }

  removeChild(node) {
    const at = this.childNodes.indexOf(node);
    if (at >= 0) this.childNodes.splice(at, 1);
    node.parentNode = null;
    return node;
  }

  insertBefore(node, reference) {
    if (reference === null || reference === undefined) return this.appendChild(node);
    const at = this.childNodes.indexOf(reference);
    if (at < 0) throw new Error('insertBefore: reference node is not a child');
    if (node.parentNode) node.parentNode.removeChild(node);
    node.parentNode = this;
    this.childNodes.splice(at, 0, node);
    return node;
  }

  get nextSibling() {
    if (!this.parentNode) return null;
    const at = this.parentNode.childNodes.indexOf(this);
    return this.parentNode.childNodes[at + 1] || null;
  }

  get firstChild() {
    return this.childNodes[0] || null;
  }

  set innerHTML(html) {
    this.childNodes = [];
    for (const node of parseFragment(String(html), this.page)) {
      this.appendChild(node);
    }
  }

  set textContent(value) {
    this.childNodes = [];
    this.appendChild(new TextNode(String(value)));
  }

  get textContent() {
    return this.childNodes.map((n) => n.textContent).join('');
  }

  addEventListener(name, handler) {
    (this.listeners[name] = this.listeners[name] || []).push(handler);
  }

  dispatch(name, event) {
    for (const handler of this.listeners[name] || []) handler(event || {});
  }

  // Every way an element can move the person off this page, recorded rather
  // than performed. The auto-advance this page had used to be a .click() on
  // the flow's Next button; form.requestSubmit() is the same thing under
  // another name, so both land in the same log.
  click() {
    this.page.movedThePerson.push(describe(this) + '.click()');
  }
  submit() {
    this.page.movedThePerson.push(describe(this) + '.submit()');
  }
  requestSubmit() {
    this.page.movedThePerson.push(describe(this) + '.requestSubmit()');
  }
  focus() {}
}

function describe(el) {
  return el.id ? '#' + el.id : el.tagName.toLowerCase();
}

// A tokenizer for the markup this page builds, which is plain nested tags with
// double quoted attributes. It is not an HTML parser and does not pretend to
// be one: anything it does not understand throws.
function parseFragment(html, page) {
  const roots = [];
  const stack = [];
  const push = (node) => {
    if (stack.length) stack[stack.length - 1].appendChild(node);
    else roots.push(node);
  };
  let i = 0;
  while (i < html.length) {
    const lt = html.indexOf('<', i);
    if (lt < 0) {
      const tail = html.slice(i);
      if (tail.trim()) push(new TextNode(tail));
      break;
    }
    if (lt > i) {
      const text = html.slice(i, lt);
      if (text.trim()) push(new TextNode(text));
    }
    const gt = html.indexOf('>', lt);
    if (gt < 0) throw new Error('fake_page: unterminated tag');
    const raw = html.slice(lt + 1, gt).trim();
    i = gt + 1;
    if (raw.startsWith('/')) {
      const open = stack.pop();
      if (!open || open.tagName !== raw.slice(1).trim().toUpperCase()) {
        throw new Error('fake_page: mismatched close tag ' + raw);
      }
      continue;
    }
    const selfClosing = raw.endsWith('/');
    const body = selfClosing ? raw.slice(0, -1) : raw;
    const nameMatch = /^([A-Za-z][A-Za-z0-9]*)/.exec(body);
    if (!nameMatch) throw new Error('fake_page: unreadable tag ' + raw);
    const el = new FakeElement(nameMatch[1], page);
    const attrs = body.slice(nameMatch[1].length);
    const attrRe = /([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*"([^"]*)"/g;
    let m;
    while ((m = attrRe.exec(attrs)) !== null) el.setAttribute(m[1], m[2]);
    push(el);
    if (!selfClosing) stack.push(el);
  }
  if (stack.length) throw new Error('fake_page: unclosed tag ' + stack[0].tagName);
  return roots;
}

// The clock the page's timers run on. Nothing here waits sixty real seconds to
// find out what the inactivity timeout does.
class Clock {
  constructor() {
    this.now = 1600000000000;
    this.seq = 0;
    this.pending = new Map();
  }
  setTimeout(fn, ms) {
    const id = ++this.seq;
    this.pending.set(id, {fn, at: this.now + (ms || 0), every: null});
    return id;
  }
  setInterval(fn, ms) {
    const id = ++this.seq;
    this.pending.set(id, {fn, at: this.now + (ms || 0), every: ms || 1});
    return id;
  }
  clear(id) {
    this.pending.delete(id);
  }
  advance(ms) {
    const target = this.now + ms;
    for (;;) {
      let next = null;
      let nextId = null;
      for (const [id, timer] of this.pending) {
        if (timer.at <= target && (next === null || timer.at < next.at)) {
          next = timer;
          nextId = id;
        }
      }
      if (next === null) break;
      this.now = next.at;
      if (next.every === null) this.pending.delete(nextId);
      else next.at = this.now + next.every;
      next.fn();
    }
    this.now = target;
  }
}

class FakeSocket {
  constructor(url, page) {
    this.url = url;
    this.page = page;
    this.sent = [];
    this.closedByPage = false;
    this.closeFired = false;
    page.sockets.push(this);
  }
  send(data) {
    this.sent.push(data);
  }
  close() {
    this.closedByPage = true;
    // A browser never fires close synchronously out of close(): the event
    // arrives after the closing handshake, which is a network round trip away.
    // Firing it inline here would hide every bug that lives in the gap, so it
    // goes on the clock like a real one.
    this.page.clock.setTimeout(() => this.fireClose(), 0);
  }
  fireOpen() {
    if (this.onopen) this.onopen({});
  }
  fireMessage(payload) {
    if (this.onmessage) {
      this.onmessage({data: typeof payload === 'string' ? payload : JSON.stringify(payload)});
    }
  }
  fireClose() {
    if (this.closeFired) return;
    this.closeFired = true;
    if (this.onclose) this.onclose({});
  }
  fireError() {
    if (this.onerror) this.onerror({});
  }
}

// The page the run happens on. The markup mirrors entity_extract.html inside
// single_optional_question.html: the yellow waiting block the extraction
// template renders, sitting inside the flow's own form next to its Next
// button. The ids are the real ones, and tests/sync/test_entity_fetcher_
// behaviour.py checks they are still the real ones, so a restored
// auto-advance has the same things to grab here that it would have in a
// browser.
const PAGE_MARKUP = `
<form id="fuck_health_insurance_form">
  <div id="waiting-msg" class="waiting-msg">
    <h4>Analyzing your denial...</h4>
  </div>
  <div class="form-navigation">
    <button id="next" type="submit">Next</button>
  </div>
</form>
`;

function buildPage() {
  const page = {sockets: [], movedThePerson: [], clock: new Clock()};
  const body = new FakeElement('body', page);
  page.body = body;
  for (const node of parseFragment(PAGE_MARKUP, page)) body.appendChild(node);

  const find = (node, id) => {
    if (node.nodeType === 1 && node.id === id) return node;
    for (const child of node.childNodes || []) {
      const hit = find(child, id);
      if (hit) return hit;
    }
    return null;
  };

  page.document = {
    body,
    getElementById: (id) => find(body, id),
    createElement: (tag) => new FakeElement(tag, page),
    addEventListener: (name, handler) => {
      (page.documentListeners = page.documentListeners || {});
      (page.documentListeners[name] = page.documentListeners[name] || []).push(handler);
    },
  };
  page.location = {
    protocol: 'https:',
    host: 'example.test',
    get href() {
      return 'https://example.test/entity/';
    },
    set href(value) {
      page.movedThePerson.push('location.href = ' + value);
    },
    assign: (url) => page.movedThePerson.push('location.assign(' + url + ')'),
    replace: (url) => page.movedThePerson.push('location.replace(' + url + ')'),
    reload: () => page.movedThePerson.push('location.reload()'),
  };
  return page;
}

// Put the page where a browser would put it, then load the compiled module so
// it binds these globals the way it binds the browser's.
function install(page) {
  global.document = page.document;
  global.location = page.location;
  global.window = {
    document: page.document,
    location: page.location,
    open: (url) => page.movedThePerson.push('window.open(' + url + ')'),
    addEventListener: () => {},
  };
  global.WebSocket = function (url) {
    return new FakeSocket(url, page);
  };
  global.setTimeout = (fn, ms) => page.clock.setTimeout(fn, ms);
  global.setInterval = (fn, ms) => page.clock.setInterval(fn, ms);
  global.clearTimeout = (id) => page.clock.clear(id);
  global.clearInterval = (id) => page.clock.clear(id);
  global.Date.now = () => page.clock.now;

  // @sentry/browser is a browser bundle and is not worth loading to run four
  // assertions; the page only ever calls captureMessage on it.
  const Module = require('module');
  const load = Module._load;
  Module._load = function (request, ...rest) {
    if (request === '@sentry/browser') {
      return {captureMessage: (msg) => page.sentry.push(msg)};
    }
    return load.call(this, request, ...rest);
  };
  page.sentry = [];
}

module.exports = {buildPage, install, FakeSocket, parseFragment};
