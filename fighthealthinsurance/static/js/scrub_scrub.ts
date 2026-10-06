import {
  setLocalStorageItemWithTTL,
  type ScrubberStorageKey,
} from "./shared";
import { takeOutTypedValues, typedValue, type TypedValue } from "./typed_value_pattern";

// The middle column is the storage key, typed so a new rule cannot store
// under a key that clearFormData does not clear.
type ScrubRegex = [RegExp, ScrubberStorageKey, string];
var scrubRegex: ScrubRegex[] = [
  [
    new RegExp("patents?:?\\s+(?<token>\\w+)", "gmi"),
    "name",
    "Patient: {{FIRST_NAME}} {{LAST_NAME}}",
  ],
  [
    new RegExp("patients?:?\\s+(?<token>\\w+)", "gmi"),
    "name",
    "Patient: {{FIRST_NAME}} {{LAST_NAME}}",
  ],
  [
    new RegExp("member:\\s+(?<token>\\w+)", "gmi"),
    "name",
    "Member: {{FIRST_NAME}} {{LAST_NAME}}",
  ],
  [
    new RegExp("member:\\s+(?<token>\\w+\\s+\\w+)", "gmi"),
    "name",
    "Member: {{FIRST_NAME}} {{LAST_NAME}}",
  ],
  [
    new RegExp("dear\\s+(?<token>\\w+\\s+\\w+)", "gmi"),
    "name",
    "Dear {{FIRST_NAME}} {{LAST_NAME}}",
  ],
  [
    new RegExp("dear\\s+(?<token>\\w+\\s+\\w+)\\s*\.?\\w+", "gmi"),
    "name",
    "Dear {{FIRST_NAME}} {{LAST_NAME}}",
  ],
  [new RegExp("dear\\s+(?<token>\\w+)", "gmi"), "name", "Dear {{FIRST_NAME}} {{LAST_NAME}}"],
  [
    new RegExp("Subscriber\\s*ID\\s*.?\\s*.?\\s*(?<token>\\w+)", "gmi"),
    "subscriber_id",
    "Subscriber ID: {{SCSID}}",
  ],
  [
    new RegExp("Group\\s*ID\\s*.?\\s*.?\\s*(?<token>\\w+)", "gmi"),
    "group_id",
    "Group ID: {{GPID}}",
  ],
  [
    new RegExp("Group\\s*.?\\s*:\\s*(?<token>\\w+)", "gmi"),
    "group_id",
    "Group ID: {{GPID}}",
  ],
  [
    new RegExp("Subscriber\\s*number\\s*.?\\s*.?\\s*(?<token>\\w+)", "gmi"),
    "subscriber_id",
    "Subscriber ID: {{SCSID}}",
  ],
  [
    new RegExp("Group\\s*number\\s*.?\\s*.?\\s*(?<token>\\w+)", "gmi"),
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

// The boxes whose value is taken out of the letter: About you, and the
// email. The letter is kept and may be read by staff, and the email only as
// "how we store it" says, so an email left in the letter got around that.
function removedFromTheLetter(node: HTMLInputElement): boolean {
  return (node.id.startsWith("store_") || node.id === "email") && typedIn(node);
}

function scrubText(text: string): string {
  var reservedTokens: [TypedValue, string][] = [];
  var nodes = document.querySelectorAll("input");
  for (let i = 0; i < nodes.length; i++) {
    var node = nodes[i];
    // What the person typed is found however the letter spaces it, and a
    // value of one word only where it stands whole: a typed "123 Sample
    // Street Apt 4B" matches the street with "Apt 4B" on the line under it,
    // a typed "283 24th St" takes out all of "283 24th Street", and a typed
    // "Ann" leaves "annual" alone (typed_value_pattern.ts).
    const typed = removedFromTheLetter(node) ? typedValue(node.value) : null;
    if (typed !== null) {
      const placeholder = storeIdToPlaceholder[node.id] || `{{${node.id}}}`;
      reservedTokens.push([typed, placeholder]);
      // Each About you box is also looked for run together with every
      // other typed box ("AnnDoe"); the email box, which main did not read,
      // only on its own.
      if (node.id === "email") {
        continue;
      }
      for (let j = 0; j < nodes.length; j++) {
        var secondNode = nodes[j];
        const together = typedIn(secondNode) ? typedValue(node.value + secondNode.value) : null;
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
  // Each value is looked for in the letter as the labels left it, and a
  // match never ends inside a word, so a placeholder no longer needs a space
  // in front of it to keep it off the rest of a word it was cut out of.
  text = takeOutTypedValues(text, reservedTokens);
  return text;
}

export function clean(): void {
  const denialText = document.getElementById(
    "denial_text",
  ) as HTMLTextAreaElement;
  denialText.value = scrubText(denialText.value);
}
