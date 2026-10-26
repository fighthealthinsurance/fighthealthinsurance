'use strict';
// Runs static/js/file_drop.js over small fake pages and prints what each drag
// and drop did, as JSON, for test_file_drop_behaviour.py. There is no jsdom in
// this repo, so the page is the handful of DOM pieces the script touches, and
// a selector the fake does not know throws rather than matching nothing.
const fs = require('fs');
const vm = require('vm');

const [, , scriptPath] = process.argv;
if (!scriptPath) {
  throw new Error('usage: file_drop_behaviour.cjs <file_drop.js>');
}
const SOURCE = fs.readFileSync(scriptPath, 'utf8');

class ClassList {
  constructor() {
    this.names = new Set();
  }
  add(name) {
    this.names.add(name);
  }
  remove(name) {
    this.names.delete(name);
  }
  contains(name) {
    return this.names.has(name);
  }
}

class FakeElement {
  constructor(tag, attrs = {}, children = []) {
    this.tagName = tag.toUpperCase();
    this.attrs = attrs;
    this.children = [];
    this.parentNode = null;
    this.classList = new ClassList();
    this.listeners = {};
    this.type = attrs.type || '';
    this.multiple = 'multiple' in attrs;
    this.disabled = 'disabled' in attrs;
    this.files = null;
    for (const child of children) {
      child.parentNode = this;
      this.children.push(child);
    }
  }
  matches(selector) {
    if (selector === '[data-file-drop]') return 'data-file-drop' in this.attrs;
    if (selector === 'input[type="file"]') return this.tagName === 'INPUT' && this.type === 'file';
    throw new Error('the fake page does not know the selector ' + selector);
  }
  closest(selector) {
    for (let node = this; node; node = node.parentNode) {
      if (node.matches(selector)) return node;
    }
    return null;
  }
  *descendants() {
    for (const child of this.children) {
      yield child;
      yield* child.descendants();
    }
  }
  querySelector(selector) {
    for (const node of this.descendants()) {
      if (node.matches(selector)) return node;
    }
    return null;
  }
  contains(other) {
    for (let node = other; node; node = node.parentNode) {
      if (node === this) return true;
    }
    return false;
  }
  addEventListener(type, listener) {
    (this.listeners[type] = this.listeners[type] || []).push(listener);
  }
  dispatchEvent(event) {
    event.target = event.target || this;
    for (let node = this; node; node = node.parentNode) {
      for (const listener of node.listeners[event.type] || []) listener.call(node, event);
      if (!event.bubbles) break;
    }
    return !event.defaultPrevented;
  }
}

class FakeDocument extends FakeElement {
  matches() {
    return false;
  }
}

class FakeEvent {
  constructor(type, init = {}) {
    this.type = type;
    this.bubbles = Boolean(init.bubbles);
    this.defaultPrevented = false;
  }
  preventDefault() {
    this.defaultPrevented = true;
  }
}

// What the script builds to hand one file of several to a box for one file.
class FakeDataTransfer {
  constructor() {
    const list = [];
    this.items = { add: (file) => list.push(file) };
    this.list = list;
  }
  get files() {
    return this.list.slice();
  }
}

const PAGE_ONE = { name: 'page-1.png' };
const PAGE_TWO = { name: 'page-2.png' };

// A page holding one box (unless withBox is false) and a paragraph outside
// it, with the script loaded the way base.html loads it.
function page({ withBox = true, multiple = true, disabled = false } = {}) {
  const inputAttrs = { type: 'file' };
  if (multiple) inputAttrs.multiple = '';
  if (disabled) inputAttrs.disabled = '';
  const input = new FakeElement('input', inputAttrs);
  const label = new FakeElement('label');
  const textarea = new FakeElement('textarea');
  const box = new FakeElement('div', { 'data-file-drop': '' }, [
    textarea,
    new FakeElement('div', {}, [input, label]),
  ]);
  const elsewhere = new FakeElement('p');
  const body = new FakeElement('body', {}, withBox ? [box, elsewhere] : [elsewhere]);
  const html = new FakeElement('html', {}, [body]);
  const document = new FakeDocument('#document', {}, [html]);
  document.documentElement = html;
  const changes = [];
  input.addEventListener('change', (event) => changes.push({ bubbles: event.bubbles }));
  vm.runInNewContext(SOURCE, { document, DataTransfer: FakeDataTransfer, Event: FakeEvent });
  return { document, box, input, label, textarea, elsewhere, changes };
}

// A drag event the way the browser fires it: at the element under the
// pointer, bubbling up to the document.
function drag(type, target, { files = [PAGE_ONE], types = ['Files'], relatedTarget = null } = {}) {
  const event = new FakeEvent(type, { bubbles: true });
  event.relatedTarget = relatedTarget;
  event.dataTransfer = { types, files, dropEffect: 'unset' };
  target.dispatchEvent(Object.assign(event, { target }));
  return { prevented: event.defaultPrevented, effect: event.dataTransfer.dropEffect };
}

function names(files) {
  return files ? Array.from(files, (file) => file.name) : null;
}

const results = {};

{
  const p = page();
  const over = drag('dragover', p.label);
  const litWhileOver = p.box.classList.contains('fhi-drop-active');
  const drop = drag('drop', p.textarea, { files: [PAGE_ONE, PAGE_TWO] });
  results.dropped_on_the_box = {
    over,
    litWhileOver,
    drop,
    files: names(p.input.files),
    changes: p.changes,
    litAfter: p.box.classList.contains('fhi-drop-active'),
  };
}

{
  const p = page();
  drag('dragenter', p.textarea);
  drag('dragleave', p.textarea, { relatedTarget: p.label });
  const litMovingWithin = p.box.classList.contains('fhi-drop-active');
  drag('dragleave', p.label, { relatedTarget: p.elsewhere });
  results.leaving_the_box = {
    litMovingWithin,
    litAfterLeaving: p.box.classList.contains('fhi-drop-active'),
  };
}

{
  const p = page();
  const over = drag('dragover', p.elsewhere);
  const drop = drag('drop', p.elsewhere);
  results.dropped_beside_the_box = { over, drop, files: names(p.input.files), changes: p.changes };
}

{
  const p = page({ withBox: false });
  results.dropped_on_a_page_with_no_box = {
    over: drag('dragover', p.elsewhere),
    drop: drag('drop', p.elsewhere),
  };
}

{
  const p = page();
  const text = { files: [], types: ['text/plain'] };
  results.text_dragged_into_the_box = {
    over: drag('dragover', p.textarea, text),
    drop: drag('drop', p.textarea, text),
    files: names(p.input.files),
    changes: p.changes,
  };
}

{
  const p = page({ disabled: true });
  results.dropped_while_the_input_is_off = {
    over: drag('dragover', p.textarea),
    drop: drag('drop', p.textarea),
    files: names(p.input.files),
    changes: p.changes,
  };
}

{
  const p = page({ multiple: false });
  drag('drop', p.textarea, { files: [PAGE_ONE, PAGE_TWO] });
  results.two_dropped_on_a_box_for_one = { files: names(p.input.files), changes: p.changes };
}

{
  const p = page();
  results.page_marked = p.document.documentElement.classList.contains('fhi-file-drop');
}

process.stdout.write(JSON.stringify(results));
