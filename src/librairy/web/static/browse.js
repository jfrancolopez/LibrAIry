// Browse keyboard navigation (P14-03): j/k or arrows move the highlight, Enter
// follows the highlighted row, Backspace goes up one level. Read-only — this
// only moves focus and follows existing links, it never mutates anything.
(function () {
  var list = document.getElementById("browse-list");
  if (!list) return;
  var rows = Array.prototype.slice.call(list.querySelectorAll(".browse-row"));
  if (!rows.length) return;
  var index = -1;

  function highlight(next) {
    if (next < 0 || next >= rows.length) return;
    if (index >= 0) rows[index].classList.remove("is-active");
    index = next;
    var row = rows[index];
    row.classList.add("is-active");
    row.focus({ preventScroll: false });
    // Item rows carry hx-get: load the detail panel without navigating away.
    var url = row.getAttribute("hx-get");
    if (window.htmx && url) {
      window.htmx.ajax("GET", url, { target: "#browse-panel", swap: "outerHTML" });
    }
  }

  document.addEventListener("keydown", function (event) {
    var tag = (event.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select") return;
    if (event.key === "j" || event.key === "ArrowDown") {
      event.preventDefault();
      highlight(index + 1 >= rows.length ? rows.length - 1 : index + 1);
    } else if (event.key === "k" || event.key === "ArrowUp") {
      event.preventDefault();
      highlight(index - 1 < 0 ? 0 : index - 1);
    } else if (event.key === "Enter" && index >= 0) {
      window.location.href = rows[index].getAttribute("href");
    } else if (event.key === "Backspace") {
      event.preventDefault();
      var parent = list.getAttribute("data-parent");
      if (parent) window.location.href = parent;
    }
  });
})();
