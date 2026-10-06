// Not main's shared.ts: a stand-in with only what the scrubbers beside it
// import, so they compile here without pdf.js. Remove personal details
// stores a name or an id it recognised through setLocalStorageItemWithTTL,
// and the fake page the differential test runs on has no storage, where the
// real helper does nothing either. The three other files in this directory
// are main's, byte for byte (see tests/js/scrub_differential.cjs).
export type ScrubberStorageKey = "name" | "subscriber_id" | "group_id";

export function setLocalStorageItemWithTTL(key: string, value: string): void {
  void key;
  void value;
}
