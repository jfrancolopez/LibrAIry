// Expand every preview on the page at once, and collapse them again.
//
// Reviewing a page of proposals one Preview click at a time is the slow way to
// answer "which of these is actually wrong". Delegated from the document so it
// keeps working after htmx swaps the list.
//
// This deliberately does NOT use htmx.ajax in a loop: htmx maintains its own
// request queue and silently drops concurrent calls, so asking it to expand
// fifty rows produced one or two. Plain fetch, with a small concurrency limit,
// is both correct and kinder to a server backed by one SQLite connection.
(function () {
  var MAX_IN_FLIGHT = 4;

  // Read the per-row Preview buttons rather than duplicating their URLs into
  // data attributes: one source of truth, and the row keeps working on its own.
  function toggles() {
    return Array.prototype.slice.call(
      document.querySelectorAll('button[hx-get^="/preview/items/"]')
    );
  }

  function targetOf(button) {
    return document.querySelector(button.getAttribute("hx-target"));
  }

  function expandAll(bar) {
    var queue = toggles().filter(function (button) {
      var target = targetOf(button);
      return target && !target.hasChildNodes();
    });
    var total = queue.length;
    if (!total) return;

    var done = 0;
    var failed = 0;
    report();

    function report() {
      var left = total - done;
      if (left > 0) {
        setStatus(bar, "loading " + left + " of " + total + "…");
      } else {
        setStatus(bar, failed ? failed + " could not be previewed" : "");
      }
    }

    function next() {
      var button = queue.shift();
      if (!button) return;
      var target = targetOf(button);
      // bulk=1 tells the server not to go looking up album art it does not
      // already have — see preview_for_item.
      fetch(button.getAttribute("hx-get") + "?bulk=1", {
        headers: { "HX-Request": "true" },
        credentials: "same-origin"
      })
        .then(function (response) {
          if (!response.ok) throw new Error(response.status);
          return response.text();
        })
        .then(function (html) {
          target.innerHTML = html;
        })
        .catch(function () {
          failed += 1;
          // Say so in the row itself. A preview that silently stays blank
          // looks like the button is broken.
          target.innerHTML =
            '<p class="muted preview-failed">Preview unavailable — the file may have moved.</p>';
        })
        .then(function () {
          done += 1;
          report();
          next();
        });
    }

    for (var i = 0; i < Math.min(MAX_IN_FLIGHT, total); i++) next();
  }

  function collapseAll(bar) {
    toggles().forEach(function (button) {
      var target = targetOf(button);
      if (target) target.replaceChildren();
    });
    setStatus(bar, "");
  }

  function setStatus(bar, text) {
    var status = bar && bar.querySelector("[data-preview-status]");
    if (status) status.textContent = text;
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-preview-all]");
    if (!button) return;
    var bar = button.closest("[data-preview-bar]");
    if (button.dataset.previewAll === "collapse") {
      collapseAll(bar);
    } else {
      expandAll(bar);
    }
  });
})();
