type ScrubRegex = [RegExp, string, string];
var scrubRegex: ScrubRegex[] = [
  [
    new RegExp("patents?:?\\s+(?<token>\\w+)", "gmi"),
    "name",
    "Patient: patient_name",
  ],
  [
    new RegExp("patients?:?\\s+(?<token>\\w+)", "gmi"),
    "name",
    "Patient: patient_name",
  ],
  [
    new RegExp("member:\\s+(?<token>\\w+)", "gmi"),
    "name",
    "Member: member_name",
  ],
  [
    new RegExp("member:\\s+(?<token>\\w+\\s+\\w+)", "gmi"),
    "name",
    "Member: member_name",
  ],
  [
    new RegExp("dear\\s+(?<token>\\w+\\s+\\w+)", "gmi"),
    "name",
    "Dear patient_name",
  ],
  [
    new RegExp("dear\\s+(?<token>\\w+\\s+\\w+)\\s*\.?\\w+", "gmi"),
    "name",
    "Dear patient_name",
  ],
  [new RegExp("dear\\s+(?<token>\\w+)", "gmi"), "name", "Dear patient_name"],
  [
    new RegExp("Subscriber\\s*ID\\s*.?\\s*.?\\s*(?<token>\\w+)", "gmi"),
    "subscriber_id",
    "Subscriber ID: subscribed_id",
  ],
  [
    new RegExp("Group\\s*ID\\s*.?\\s*.?\\s*(?<token>\\w+)", "gmi"),
    "group_id",
    "Group ID: group_id",
  ],
  [
    new RegExp("Group\\s*.?\\s*:\\s*(?<token>\\w+)", "gmi"),
    "group_id",
    "Group ID: group_id",
  ],
  [
    new RegExp("Subscriber\\s*number\\s*.?\\s*.?\\s*(?<token>\\w+)", "gmi"),
    "subscriber_id",
    "Subscriber ID: subscribed_id",
  ],
  [
    new RegExp("Group\\s*number\\s*.?\\s*.?\\s*(?<token>\\w+)", "gmi"),
    "group_id",
    "Group ID: group_id",
  ],
];

// Helper function to escape special regex characters in a string
function escapeRegExp(string: string): string {
  // Escapes special characters in a string to safely use it inside a RegExp
  // $& inserts the matched character, and \\ escapes it
  return string.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function scrubText(text: string): string {
  var reservedTokens = [];
  var nodes = document.querySelectorAll("input");
  for (let i = 0; i < nodes.length; i++) {
    var node = nodes[i];
    if (node.id.startsWith("store_") && node.value != "") {
      reservedTokens.push([
        new RegExp(escapeRegExp(node.value), "gi"),
        node.id,
      ]);
      for (let j = 0; j < nodes.length; j++) {
        var secondNode = nodes[j];
        if (secondNode.value != "") {
          reservedTokens.push([
            new RegExp(escapeRegExp(node.value + secondNode.value), "gi"),
            node.id + "_" + secondNode.id,
          ]);
        }
      }
    }
  }
  console.log("Preparing to scrub:");
  console.log(text);
  console.log("Scrubbing with:");
  console.log(reservedTokens);
  console.log(scrubRegex);
  for (let i = 0; i < scrubRegex.length; i++) {
    const match = scrubRegex[i][0].exec(text);
    if (match !== null) {
      // I want to use the groups syntax here but it is not working so just index in I guess.
      console.log("Match " + match + " groups " + match[1]);
      console.log("Storing " + match[1] + " for " + scrubRegex[i][1]);
      window.localStorage.setItem(scrubRegex[i][1], match[1]);
    }
    text = text.replace(scrubRegex[i][0], scrubRegex[i][2]);
  }
  for (let i = 0; i < reservedTokens.length; i++) {
    text = text.replace(reservedTokens[i][0], " " + reservedTokens[i][1]);
  }
  return text;
}

export function clean(): void {
  const denialText = document.getElementById(
    "denial_text",
  ) as HTMLTextAreaElement;
  denialText.value = scrubText(denialText.value);
}
