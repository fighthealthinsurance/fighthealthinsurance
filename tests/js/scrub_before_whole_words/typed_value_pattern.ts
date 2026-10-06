// A value the person typed (their street, their name) as a regular
// expression source that finds it in a letter however the letter spaces it:
// each word escaped, and any run of whitespace between words (spaces, a tab,
// a line break) taken as equal. A letter often puts "Apt 4B" on the line
// under "123 Sample Street", so a typed "123 Sample Street Apt 4B" matched
// with its literal spaces was left in the letter. Used by Remove personal
// details (scrub_scrub.ts) and by the chat's scrubPersonalInfo
// (user_info_storage.ts). Null for a value with no words, which would match
// everywhere.

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function typedValuePattern(value: string): string | null {
  const words = value.split(/\s+/).filter((word) => word !== "");
  return words.length === 0 ? null : words.map(escapeRegExp).join("\\s+");
}
