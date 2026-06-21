// Fills the SiteBanner "message" box from the pre-canned message picker in the
// Django admin. Selecting a template copies its text into the message textarea
// (confirming first if the box already has content), then resets the picker so
// staff can quickly start from a ready-made notice and edit as needed.
(function () {
  "use strict";

  function init() {
    var picker = document.getElementById("id_canned_message");
    var message = document.getElementById("id_message");
    if (!picker || !message) {
      return;
    }
    picker.addEventListener("change", function () {
      var text = picker.value;
      if (!text) {
        return;
      }
      if (
        message.value.trim() &&
        !window.confirm("Replace the current message with the selected template?")
      ) {
        picker.value = "";
        return;
      }
      message.value = text;
      picker.value = "";
      message.focus();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
