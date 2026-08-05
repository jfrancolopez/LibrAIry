// Expand every preview on the page at once, and collapse them again.
//
// Reviewing a page of proposals one Preview click at a time is the slow way to
// answer "which of these is actually wrong". Delegated from the document so it
// keeps working after htmx swaps the list.
(function () {
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
    var pending = toggles().filter(function (button) {
      var target = targetOf(button);
      return target && !target.hasChildNodes();
    });
    if (!pending.length) return;

    var done = 0;
    setStatus(bar, "loading " + pending.length + "…");
    pending.forEach(function (button) {
      var target = targetOf(button);
      // bulk=1 tells the server not to go looking up album art it does not
      // already have — see preview_for_item.
      window.htmx.ajax("GET", button.getAttribute("hx-get") + "?bulk=1", target).then(function () {
        done += 1;
        setStatus(bar, done < pending.length ? "loading " + (pending.length - done) + "…" : "");
      });
    });
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
