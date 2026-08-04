// Explorer keyboard navigation (Phase 17).
// Up/Down move within a pane, Left/Right change pane, Enter follows the
// highlighted row, Backspace goes up one folder. Read-only: this only moves
// focus and follows links that already exist in the markup.
(function () {
  var explorer = document.getElementById("explorer");
  if (!explorer) return;

  var panes = Array.prototype.slice
    .call(explorer.querySelectorAll(".pane-list"))
    .map(function (list) {
      return {
        list: list,
        rows: Array.prototype.slice.call(list.querySelectorAll(".browse-row")),
      };
    })
    .filter(function (pane) {
      return pane.rows.length > 0;
    });
  if (!panes.length) return;

  var paneIndex = panes.length > 1 ? 1 : 0; // start on Folders when present
  var rowIndex = -1;

  function clearHighlight() {
    explorer.querySelectorAll(".browse-row.is-active").forEach(function (row) {
      row.classList.remove("is-active");
    });
  }

  function highlight(nextPane, nextRow) {
    var pane = panes[nextPane];
    if (!pane || !pane.rows.length) return;
    nextRow = Math.max(0, Math.min(nextRow, pane.rows.length - 1));
    clearHighlight();
    paneIndex = nextPane;
    rowIndex = nextRow;
    var row = pane.rows[rowIndex];
    row.classList.add("is-active");
    // focus() drives the htmx "focus" trigger on file rows, which loads the
    // detail pane as you arrow through the list.
    row.focus({ preventScroll: false });
    row.scrollIntoView({ block: "nearest" });
  }

  document.addEventListener("keydown", function (event) {
    var tag = (event.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select") return;
    if (event.metaKey || event.ctrlKey || event.altKey) return;

    switch (event.key) {
      case "ArrowDown":
      case "j":
        event.preventDefault();
        highlight(paneIndex, rowIndex + 1);
        break;
      case "ArrowUp":
      case "k":
        event.preventDefault();
        highlight(paneIndex, rowIndex - 1);
        break;
      case "ArrowRight":
      case "l":
        event.preventDefault();
        highlight(Math.min(paneIndex + 1, panes.length - 1), 0);
        break;
      case "ArrowLeft":
      case "h":
        event.preventDefault();
        highlight(Math.max(paneIndex - 1, 0), 0);
        break;
      case "Enter":
        if (rowIndex >= 0) {
          var current = panes[paneIndex].rows[rowIndex];
          if (current) window.location.href = current.getAttribute("href");
        }
        break;
      case "Backspace": {
        event.preventDefault();
        var withParent = explorer.querySelector("[data-parent]");
        var parent = withParent && withParent.getAttribute("data-parent");
        if (parent) window.location.href = parent;
        break;
      }
      default:
        break;
    }
  });
})();
