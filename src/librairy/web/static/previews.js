// Inline previews: one row at a time, or a whole page at once.
//
// Two things live here because they are the same thing seen twice. A Preview
// button opens the panel under its row and closes it again; "Preview all" is
// that same open, applied to every row that is closed. Splitting them meant
// the row button was htmx and the bulk button was fetch, and they disagreed
// about what "open" meant — which is how Preview came to be a control that
// only went one way. Clicking it a second time re-fetched the same panel and
// swapped it over itself, so the button looked inert and the only way to close
// a preview was to reload the page.
//
// This deliberately does NOT use htmx.ajax in a loop: htmx maintains its own
// request queue and silently drops concurrent calls, so asking it to expand
// fifty rows produced one or two. Plain fetch, with a small concurrency limit,
// is both correct and kinder to a server backed by one SQLite connection.
(function () {
  var MAX_IN_FLIGHT = 4;

  function toggles() {
    return Array.prototype.slice.call(document.querySelectorAll("[data-preview-toggle]"));
  }

  function targetOf(button) {
    return document.getElementById(button.dataset.previewTarget);
  }

  function isOpen(target) {
    return !!target && target.hasChildNodes();
  }

  // Closing is not hiding. A `<video>` left in the DOM with `display: none`
  // keeps its decoder, its buffer and — on more browsers than you would like —
  // its audio. The fullscreen viewer learned this the hard way; the same
  // teardown applies here.
  function close(button) {
    var target = targetOf(button);
    if (!target) return;
    target.querySelectorAll("video, audio").forEach(function (media) {
      try {
        media.pause();
        media.removeAttribute("src");
        media.querySelectorAll("source").forEach(function (source) {
          source.removeAttribute("src");
        });
        media.load();
      } catch (err) {
        /* a browser that refuses to be torn down is still better off paused */
      }
    });
    target.replaceChildren();
    mark(button, false);
  }

  // The label changes with the state. A button whose text never moves is a
  // button people press twice and then stop trusting.
  function mark(button, open) {
    button.setAttribute("aria-expanded", open ? "true" : "false");
    button.textContent = open ? "Hide preview" : "Preview";
  }

  function open(button, options) {
    var target = targetOf(button);
    if (!target) return Promise.resolve();
    // bulk=1 tells the server not to go looking up album art it does not
    // already have — see preview_for_item.
    var url = button.dataset.previewUrl + (options && options.bulk ? "?bulk=1" : "");
    button.disabled = true;
    return fetch(url, { headers: { "HX-Request": "true" }, credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) throw new Error(response.status);
        return response.text();
      })
      .then(function (html) {
        target.innerHTML = html;
        mark(button, true);
      })
      .catch(function () {
        // Say so in the row itself. A preview that silently stays blank looks
        // like the button is broken.
        target.innerHTML =
          '<p class="muted preview-failed">Preview unavailable — the file may have moved.</p>';
        mark(button, true);
        throw new Error("preview failed");
      })
      .then(
        function () {
          button.disabled = false;
        },
        function (err) {
          button.disabled = false;
          throw err;
        }
      );
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-preview-toggle]");
    if (!button) return;
    event.preventDefault();
    if (isOpen(targetOf(button))) close(button);
    else open(button).catch(function () {});
  });

  function expandAll(bar) {
    var queue = toggles().filter(function (button) {
      return !isOpen(targetOf(button));
    });
    var total = queue.length;
    if (!total) return;

    var done = 0;
    var failed = 0;
    report();

    function report() {
      var left = total - done;
      setStatus(bar, left > 0
        ? "loading " + left + " of " + total + "…"
        : failed ? failed + " could not be previewed" : "");
    }

    function next() {
      var button = queue.shift();
      if (!button) return;
      open(button, { bulk: true })
        .catch(function () {
          failed += 1;
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
    toggles().forEach(close);
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
    if (button.dataset.previewAll === "collapse") collapseAll(bar);
    else expandAll(bar);
  });

  // An htmx swap can replace a row whose preview was open, leaving a button
  // that says "Hide preview" over an empty panel.
  document.body.addEventListener("htmx:afterSwap", function () {
    toggles().forEach(function (button) {
      mark(button, isOpen(targetOf(button)));
    });
  });
})();
