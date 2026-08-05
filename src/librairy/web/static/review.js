// Select-all for the Review table, plus a live count of what is selected.
//
// The bulk buttons ("Approve Selected") were only reachable by ticking every
// row by hand, and the only shortcut was "select all matching current filter",
// which is a much bigger and scarier commitment than "these ones, here".
//
// Delegated from the document so it survives htmx swapping the list.
(function () {
  function rowsUnder(header) {
    var table = header.closest("table");
    return table
      ? Array.prototype.slice.call(table.querySelectorAll('tbody input[name="proposal_id"]'))
      : [];
  }

  function allBoxes() {
    return Array.prototype.slice.call(
      document.querySelectorAll('#review-list tbody input[name="proposal_id"]')
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

  document.body.addEventListener("htmx:afterSwap", function () {
    syncHeaders();
    refreshCount();
  });

  syncHeaders();
  refreshCount();
})();
