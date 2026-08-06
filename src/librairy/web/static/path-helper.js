// Storage-path .env generator. Docker bind mounts are fixed at launch, so this
// cannot remount live — it builds the four lines to paste and re-launch.
//
// There is no folder browser, and that is not an omission. A web page cannot
// open a native picker on the machine running the container: browsers hide real
// paths from pages on purpose, and the container can only see the folders
// Docker already handed it. The next best thing is typing one parent folder and
// having the four boxes fill themselves, which is what this does.
(function () {
  var helper = document.getElementById("path-helper");
  if (!helper) return;
  var fields = Array.prototype.slice.call(helper.querySelectorAll(".path-field"));
  var out = document.getElementById("path-env");
  var base = document.getElementById("path-base");
  var preset = document.getElementById("path-base-preset");
  var copy = document.getElementById("path-copy");

  function render() {
    out.value = fields
      .map(function (f) {
        return f.getAttribute("data-key") + "=" + f.value.trim();
      })
      .join("\n");
  }

  function applyBase() {
    var parent = base.value.trim().replace(/\/+$/, "");
    if (!parent) return;
    fields.forEach(function (f) {
      f.value = parent + "/" + f.getAttribute("data-leaf");
    });
    render();
  }

  fields.forEach(function (f) {
    f.addEventListener("input", render);
  });

  if (base) base.addEventListener("input", applyBase);

  if (preset) {
    preset.addEventListener("change", function () {
      if (!preset.value) return;
      base.value = preset.value;
      applyBase();
      // The placeholders are not paths anyone actually has, so send them
      // straight to the part that needs replacing rather than letting it be
      // copied into a .env as-is.
      var placeholder = base.value.indexOf("YOUR-NAME");
      if (placeholder !== -1) {
        base.focus();
        base.setSelectionRange(placeholder, placeholder + "YOUR-NAME".length);
      }
    });
  }

  if (copy) {
    copy.addEventListener("click", function () {
      out.select();
      try {
        document.execCommand("copy");
        copy.textContent = "Copied";
        setTimeout(function () {
          copy.textContent = "Copy these four lines";
        }, 1500);
      } catch (e) {
        /* selection is enough if clipboard is blocked */
      }
    });
  }

  render();
})();
