'use strict';
// Run what takes the person's details back out of a letter, compiled from
// the real TypeScript, and report what it leaves. Driven by
// tests/sync/test_scrub_whole_words_behaviour.py.
//
//   node scrub_whole_words_behaviour.cjs <compiled typed_value_pattern.js> '<spec json>'
//
// scrub_scrub.js, shared.js and user_info_storage.js sit beside it. The spec
// is one of:
//   {"find": [[value, text, kind?], ...]}
//        each text with every match of what was typed (typedValueRegExp,
//        through replaceTypedValue) put in [[double brackets]], or null
//        where the value is not looked for at all; kind is "street" or
//        "zip" where given
//   {"fullName": [[first name, last name, order, text], ...]}
//        the same for fullNameRegExp, order "first last" or "last, first"
//   {"remove": {"inputs": [{id, type, value}, ...], "cases": [[typed, letter], ...]}}
//        each letter after Remove personal details (scrub_scrub.ts clean),
//        on a page with the intake page's inputs: every one, with the value
//        the page gives it, then {field id: value} typed over them
//   {"chat": [[message, userInfo], ...]}
//        what the chat's scrubPersonalInfo makes of each message
// With "noLookbehind": true, a regular expression with a lookbehind in it
// throws when it is made, as it does in Safari before 16.4.
// Writes one JSON object to stdout, with every console call recorded.

const path = require('path');
const Module = require('module');
const {buildPage, install} = require(path.join(__dirname, 'fake_page.cjs'));

const [, , modulePath, specJson] = process.argv;
if (!modulePath || !specJson) {
  throw new Error("usage: scrub_whole_words_behaviour.cjs <module> '<spec json>'");
}
const spec = JSON.parse(specJson);
const built = path.dirname(path.resolve(modulePath));

if (spec.noLookbehind) {
  const NativeRegExp = RegExp;
  const OldSafariRegExp = function (pattern, flags) {
    const source = pattern instanceof NativeRegExp ? pattern.source : String(pattern);
    if (/\(\?<[=!]/.test(source)) {
      throw new SyntaxError('Invalid regular expression: invalid group specifier name');
    }
    return new NativeRegExp(pattern, flags);
  };
  OldSafariRegExp.prototype = NativeRegExp.prototype;
  global.RegExp = OldSafariRegExp;
}

const logs = [];
for (const level of ['debug', 'log', 'info', 'warn', 'error', 'trace', 'dir', 'table']) {
  console[level] = (...args) => logs.push([level].concat(args.map((a) => String(a))));
}

if (spec.find || spec.fullName) {
  const {fullNameRegExp, replaceTypedValue, typedValueRegExp} = require(path.resolve(modulePath));
  const mark = (typed, text) =>
    typed === null ? null : replaceTypedValue(text, typed, (match) => '[[' + match + ']]');
  const found = spec.find
    ? spec.find.map(([value, text, kind]) => mark(typedValueRegExp(value, kind), text))
    : spec.fullName.map(([first, last, order, text]) => mark(fullNameRegExp(first, last, order), text));
  process.stdout.write(JSON.stringify({found, logs}) + '\n');
  process.exit(0);
}

if (spec.chat) {
  const page = buildPage('<div></div>');
  install(page);
  const {scrubPersonalInfo} = require(path.join(built, 'user_info_storage.js'));
  const scrubbed = spec.chat.map(([message, userInfo]) => scrubPersonalInfo(message, userInfo));
  process.stdout.write(JSON.stringify({scrubbed, logs}) + '\n');
  process.exit(0);
}

if (spec.remove) {
  const page = buildPage('<form id="fuck_health_insurance_form"><textarea id="denial_text"></textarea></form>');
  install(page);
  const doc = page.document;
  const form = doc.getElementById('fuck_health_insurance_form');
  const inputs = spec.remove.inputs.map(({id, type, value}) => {
    const input = doc.createElement('input');
    if (id) input.id = id;
    if (type) input.type = type;
    form.appendChild(input);
    return {input, value: value || ''};
  });
  // shared.js loads pdf.js, which this never reaches.
  const load = Module._load;
  Module._load = function (request, ...rest) {
    if (request === 'pdfjs-dist') return {GlobalWorkerOptions: {}};
    return load.call(this, request, ...rest);
  };
  const {clean} = require(path.join(built, 'scrub_scrub.js'));
  const box = doc.getElementById('denial_text');
  const letters = spec.remove.cases.map(([typed, letter]) => {
    for (const {input, value} of inputs) {
      input.value = Object.prototype.hasOwnProperty.call(typed, input.id) ? typed[input.id] : value;
    }
    box.value = letter;
    clean();
    return box.value;
  });
  process.stdout.write(JSON.stringify({letters, logs}) + '\n');
  process.exit(0);
}

throw new Error('unknown spec ' + specJson);
