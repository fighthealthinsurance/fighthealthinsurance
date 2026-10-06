import {
  setLocalStorageItemWithTTL,
  type ScrubberStorageKey,
} from "./shared";
import { replaceTypedValue, typedValueRegExp, WORD_CHARACTERS } from "./typed_value_pattern";

// A rule's label starts a word, so "inpatient care" and "outpatient
// services" are not read as a "patient" label and their next word taken
// for the person's name. The labels are English words, so \b, which knows
// only A to Z, is enough to find where one starts. The word after a label
// is taken whole in any script: \w in a rule is a letter, a mark, a digit
// or an underscore (the u flag; JavaScript's own \w is A to Z), so "Dear
// José" takes "José", not "Jos" with its "é" left behind.
function labelRule(source: string): RegExp {
  return new RegExp("\\b" + source.replace(/\\w/g, `[${WORD_CHARACTERS}_]`), "gmiu");
}

// The middle column is the storage key, typed so a new rule cannot store
// under a key that clearFormData does not clear.
type ScrubRegex = [RegExp, ScrubberStorageKey, string];
var scrubRegex: ScrubRegex[] = [
  [
    labelRule("patents?:?\\s+(?<token>\\w+)"),
    "name",
    "Patient: {{FIRST_NAME}} {{LAST_NAME}}",
  ],
  [
    labelRule("patients?:?\\s+(?<token>\\w+)"),
    "name",
    "Patient: {{FIRST_NAME}} {{LAST_NAME}}",
  ],
  [
    labelRule("member:\\s+(?<token>\\w+)"),
    "name",
    "Member: {{FIRST_NAME}} {{LAST_NAME}}",
  ],
  [
    labelRule("member:\\s+(?<token>\\w+\\s+\\w+)"),
    "name",
    "Member: {{FIRST_NAME}} {{LAST_NAME}}",
  ],
  [
    labelRule("dear\\s+(?<token>\\w+\\s+\\w+)"),
    "name",
    "Dear {{FIRST_NAME}} {{LAST_NAME}}",
  ],
  [
    labelRule("dear\\s+(?<token>\\w+\\s+\\w+)\\s*\.?\\w+"),
    "name",
    "Dear {{FIRST_NAME}} {{LAST_NAME}}",
  ],
  [labelRule("dear\\s+(?<token>\\w+)"), "name", "Dear {{FIRST_NAME}} {{LAST_NAME}}"],
  [
    labelRule("Subscriber\\s*ID\\s*.?\\s*.?\\s*(?<token>\\w+)"),
    "subscriber_id",
    "Subscriber ID: {{SCSID}}",
  ],
  [
    labelRule("Group\\s*ID\\s*.?\\s*.?\\s*(?<token>\\w+)"),
    "group_id",
    "Group ID: {{GPID}}",
  ],
  [
    labelRule("Group\\s*.?\\s*:\\s*(?<token>\\w+)"),
    "group_id",
    "Group ID: {{GPID}}",
  ],
  [
    labelRule("Subscriber\\s*number\\s*.?\\s*.?\\s*(?<token>\\w+)"),
    "subscriber_id",
    "Subscriber ID: {{SCSID}}",
  ],
  [
    labelRule("Group\\s*number\\s*.?\\s*.?\\s*(?<token>\\w+)"),
    "group_id",
    "Group ID: {{GPID}}",
  ],
];

// Mapping from store_* input IDs to {{PLACEHOLDER}} format
const storeIdToPlaceholder: Record<string, string> = {
  store_fname: "{{FIRST_NAME}}",
  store_lname: "{{LAST_NAME}}",
  store_street: "{{ADDRESS}}",
  store_city: "{{CITY}}",
  store_state: "{{STATE}}",
  store_zip: "{{ZIP_CODE}}",
  email: "{{Your Email Address}}",
  email_address: "{{Your Email Address}}",
  subscriber_id: "{{SCSID}}",
  group_id: "{{GPID}}",
  phone_number: "{{Your Phone Number}}",
};

// The boxes a person types in. A tick box's value is set by the page, not
// typed: store_raw_email's is "checked", and taken out of the letter it
// turned every "checked" in it into a placeholder.
const TYPED_INPUT_TYPES = ["text", "email", "tel", "search", "number"];

function typedIn(node: HTMLInputElement): boolean {
  return TYPED_INPUT_TYPES.indexOf(node.type) >= 0 && node.value !== "";
}

function scrubText(text: string): string {
  const reservedTokens: [RegExp, string][] = [];
  var nodes = document.querySelectorAll("input");
  for (let i = 0; i < nodes.length; i++) {
    var node = nodes[i];
    // What the person typed is found however the letter spaces it, and only
    // as whole words: a typed "123 Sample Street Apt 4B" matches the street
    // with "Apt 4B" on the line under it, and a typed "Ann" leaves "annual"
    // alone (typed_value_pattern.ts).
    const typed = node.id.startsWith("store_") && typedIn(node) ? typedValueRegExp(node.value) : null;
    if (typed !== null) {
      const placeholder = storeIdToPlaceholder[node.id] || `{{${node.id}}}`;
      reservedTokens.push([typed, placeholder]);
      for (let j = 0; j < nodes.length; j++) {
        var secondNode = nodes[j];
        const together = typedIn(secondNode) ? typedValueRegExp(node.value + secondNode.value) : null;
        if (together !== null) {
          const secondPlaceholder = storeIdToPlaceholder[secondNode.id] || `{{${secondNode.id}}}`;
          reservedTokens.push([together, placeholder + " " + secondPlaceholder]);
        }
      }
    }
  }
  // Log only sizes: the raw text and the reserved-token regexes contain PII.
  console.debug(
    "scrub: text length",
    text.length,
    "reserved tokens",
    reservedTokens.length,
    "rules",
    scrubRegex.length,
  );
  for (let i = 0; i < scrubRegex.length; i++) {
    const match = scrubRegex[i][0].exec(text);
    if (match !== null) {
      // I want to use the groups syntax here but it is not working so just index in I guess.
      // Don't log the match itself -- it is the patient name/ID being scrubbed.
      console.debug("scrub: rule matched, storing under", scrubRegex[i][1]);
      // Through the same helper as every other field on the page, so this
      // respects the "Remember form data" setting and carries the same
      // expiry. A bare setItem here wrote the name or the member id to the
      // browser whatever the person had chosen.
      setLocalStorageItemWithTTL(scrubRegex[i][1], match[1]);
    }
    text = text.replace(scrubRegex[i][0], scrubRegex[i][2]);
  }
  // A match is whole words, so a placeholder no longer needs a space in
  // front of it to keep it off the rest of a word it was cut out of.
  for (let i = 0; i < reservedTokens.length; i++) {
    text = replaceTypedValue(text, reservedTokens[i][0], reservedTokens[i][1]);
  }
  return text;
}

export function clean(): void {
  const denialText = document.getElementById(
    "denial_text",
  ) as HTMLTextAreaElement;
  denialText.value = scrubText(denialText.value);
}
