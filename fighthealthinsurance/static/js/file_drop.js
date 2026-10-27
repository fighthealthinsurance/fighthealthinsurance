// Every box on the site that takes a file (data-file-drop) takes it dropped
// as well as chosen. A dropped file is handed to the box's own file input and
// that input's change event fired, so it takes exactly the path a chosen file
// takes: read on the device where the page reads it, sent with the form where
// the form sends it. Loaded on every page (base.html); a page with no such
// box is left to the browser.
(function () {
  'use strict';
  var ACTIVE = 'fhi-drop-active';

  // A drag that carries files, as opposed to text dragged into or within a
  // box, which is left to the browser as it always was.
  function carriesFiles(event) {
    return event.dataTransfer != null &&
      Array.prototype.indexOf.call(event.dataTransfer.types, 'Files') !== -1;
  }

  // The box under a drag and the file input it hands a drop to, or null where
  // there is none or its input cannot take a file right now.
  function boxAt(target) {
    var zone = target && target.closest ? target.closest('[data-file-drop]') : null;
    var input = zone ? zone.querySelector('input[type="file"]') : null;
    return input && !input.disabled ? { zone: zone, input: input } : null;
  }

  function pageHasBox() {
    return document.querySelector('[data-file-drop]') !== null;
  }

  var lit = null;
  function light(zone) {
    if (lit === zone) return;
    if (lit) lit.classList.remove(ACTIVE);
    lit = zone;
    if (zone) zone.classList.add(ACTIVE);
  }

  // Over a box the drop is offered; anywhere else on a page with a box it is
  // refused, because the browser would otherwise open the file in place of
  // the page and take everything typed so far with it. Without the
  // preventDefault the browser refuses a drop on the box as well.
  function offer(event) {
    if (!carriesFiles(event)) return;
    var box = boxAt(event.target);
    if (!box && !pageHasBox()) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = box ? 'copy' : 'none';
    light(box ? box.zone : null);
  }
  document.addEventListener('dragenter', offer);
  document.addEventListener('dragover', offer);
  document.addEventListener('dragleave', function (event) {
    // Moving from one part of a box to another is not leaving it. A browser
    // that does not say where the drag went gets the highlight back from the
    // next dragover.
    if (lit && !lit.contains(event.relatedTarget)) light(null);
  });
  document.addEventListener('drop', function (event) {
    light(null);
    if (!carriesFiles(event)) return;
    var box = boxAt(event.target);
    if (!box && !pageHasBox()) return;
    event.preventDefault();
    var files = event.dataTransfer.files;
    if (!box || files.length === 0) return;
    // A box for one file takes the first, as its picker would.
    if (!box.input.multiple && files.length > 1) {
      var first = new DataTransfer();
      first.items.add(files[0]);
      files = first.files;
    }
    box.input.files = files;
    // Setting files fires nothing on its own.
    box.input.dispatchEvent(new Event('change', { bubbles: true }));
  });

  // The words that ask for a drop show only once this has run (custom.css):
  // without it a file let go on a box would be opened in place of the page.
  document.documentElement.classList.add('fhi-file-drop');
})();
