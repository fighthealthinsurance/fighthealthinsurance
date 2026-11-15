// A hello for people who open the console; its twin is a comment at the top of the page source.
(function () {
  if (!window.console || typeof console.log !== "function") {
    return;
  }
  var dog = [
    "   / \\__",
    "  (    @\\___",
    "  /         O",
    " /   (_____/",
    "/_____/   U",
  ].join("\n");
  console.log("%c" + dog, "font-family: monospace; color: #f02eaa;");
  console.log(
    "Woof. You opened the console. Professor Timbit approves of people who look under the hood.",
  );
  console.log(
    "Fight Health Insurance is open source. Come help: https://github.com/fighthealthinsurance/fighthealthinsurance",
  );
  console.log("(There is another Timbit in the page source.)");
})();
