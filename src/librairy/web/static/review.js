// Select-all for the Review table, plus a live count of what is selected.
//
// The bulk buttons ("Approve Selected") were only reachable by ticking every
// row by hand, and the only shortcut was "select all matching current filter",
// which is a much bigger and scarier commitment than "these ones, here".
//
// Delegated from the document so it survives htmx swapping the list.
(function () {
  // A select-all in a group heading covers that group; the one in the toolbar
  // sits outside every group and so covers the page.
  function rowsUnder(header) {
    var scope = header.closest(".review-group") || document.getElementById("review-list");
    return scope
      ? Array.prototype.slice.call(scope.querySelectorAll('input[name="proposal_id"]'))
      : [];
  }

  function allBoxes() {
    return Array.prototype.slice.call(
      document.querySelectorAll('#review-list input[name="proposal_id"]')
    );
  }

  function refreshCount() {
    var label = document.querySelector("[data-selected-count]");
    if (!label) return;
    var selected = allBoxes().filter(function (box) {
      return box.checked;
    }).length;
    label.textContent = selected ? selected + " selected" : "";
    document.querySelectorAll("[data-needs-selection]").forEach(function (button) {
      button.disabled = selected === 0;
    });
    // Five disabled buttons held a permanent line in a sticky bar for a
    // selection that does not exist yet. They appear when they can be used.
    document.querySelectorAll("[data-selection-only]").forEach(function (group) {
      group.hidden = selected === 0;
    });
  }

  function syncHeaders() {
    document.querySelectorAll(".select-all").forEach(function (header) {
      var boxes = rowsUnder(header);
      var checked = boxes.filter(function (box) {
        return box.checked;
      }).length;
      header.checked = boxes.length > 0 && checked === boxes.length;
      // The in-between state, so a partial selection does not read as "none".
      header.indeterminate = checked > 0 && checked < boxes.length;
    });
  }

  document.addEventListener("change", function (event) {
    var target = event.target;
    if (target.classList && target.classList.contains("select-all")) {
      var checked = target.checked;
      rowsUnder(target).forEach(function (box) {
        box.checked = checked;
      });
    } else if (!(target.name === "proposal_id")) {
      return;
    }
    syncHeaders();
    refreshCount();
  });

  // "Change…" and "Cancel" open and close the edit panel. A <details> would
  // need no script, but its disclosure triangle and default typography are
  // exactly the "ugly" the edit form was called out for; this is a button that
  // looks like the other buttons. Delegated, so it survives an htmx swap.
  document.addEventListener("click", function (event) {
    var trigger = event.target.closest("[data-panel-toggle]");
    if (!trigger) return;
    var panel = document.getElementById(trigger.dataset.panelToggle);
    if (!panel) return;
    panel.hidden = !panel.hidden;
    if (panel.hidden) return;
    // data-panel-focus names the field this particular trigger is about, so
    // clicking a destination path lands the cursor in the destination box
    // rather than in the category menu that happens to come first.
    var wanted = trigger.dataset.panelFocus;
    var field =
      (wanted && panel.querySelector('[name="' + wanted + '"]')) ||
      panel.querySelector("select, input");
    if (!field) return;
    field.focus();
    if (field.select) field.select();
  });

  // htmx does not swap error responses, so a Preview button whose file has
  // gone left the panel empty and looked like a broken button. Expand-all
  // already said so; a single click should too.
  document.body.addEventListener("htmx:responseError", function (event) {
    var target = event.detail.target;
    if (!target || !target.classList.contains("proposal-preview")) return;
    var reason =
      event.detail.xhr.status === 404
        ? "Preview unavailable — the file has moved or been removed."
        : "Preview failed (" + event.detail.xhr.status + ").";
    target.innerHTML = '<p class="muted preview-failed"></p>';
    target.firstChild.textContent = reason;
  });

  document.body.addEventListener("htmx:afterSwap", function () {
    syncHeaders();
    refreshCount();
  });

  syncHeaders();
  refreshCount();
})();
