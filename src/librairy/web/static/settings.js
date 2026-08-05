// Sticky save bar: reveal only once the main settings form is dirty. Vanilla
// JS, no framework — the bar's button is a normal submit, so the page still
// saves with scripting disabled (the bar just stays visible in that case).
(function () {
  var form = document.getElementById("settings-form");
  var bar = document.getElementById("settings-save-bar");
  if (!form || !bar) return;

  function serialize() {
    var pairs = [];
    new FormData(form).forEach(function (value, key) {
      pairs.push(key + "=" + value);
    });
    return pairs.sort().join("&");
  }

  var clean = serialize();
  function refresh() {
    bar.hidden = serialize() === clean;
  }

  form.addEventListener("input", refresh);
  form.addEventListener("change", refresh);

  var discard = document.getElementById("settings-discard");
  if (discard) {
    discard.addEventListener("click", function () {
      form.reset();
      refresh();
    });
  }
})();

// Clicking a model reported by LM Studio fills the model box. The buttons
// arrive with an htmx swap, so the listener is delegated from the document
// rather than bound to elements that do not exist at load time.
(function () {
  document.addEventListener("click", function (event) {
    var button = event.target.closest(".model-pick");
    if (!button) return;
    var field = document.getElementById("lmstudio-model");
    if (!field) return;
    field.value = button.dataset.model;
    document.querySelectorAll(".model-pick").forEach(function (other) {
      other.classList.toggle("is-current", other === button);
    });
    field.dispatchEvent(new Event("input", { bubbles: true }));
  });
})();
