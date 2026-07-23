// Storage-path .env generator (P16-06). Docker bind mounts are fixed at launch,
// so this can't remount live — it builds a .env snippet to paste and re-launch.
// Vanilla JS, no secrets, no network.
(function () {
  var helper = document.getElementById("path-helper");
  if (!helper) return;
  var fields = Array.prototype.slice.call(helper.querySelectorAll(".path-field"));
  var out = document.getElementById("path-env");
  var example = document.getElementById("path-example");
  var copy = document.getElementById("path-copy");

  function render() {
    out.value = fields
      .map(function (f) {
        return f.getAttribute("data-key") + "=" + f.value.trim();
      })
      .join("\n");
  }

  fields.forEach(function (f) {
    f.addEventListener("input", render);
  });

  if (example) {
    example.addEventListener("click", function () {
      var base = "/Users/you/Desktop";
      var map = {
        HOST_INBOX_DIR: base + "/librairy-inbox",
        HOST_LIBRARY_DIR: base + "/librairy-library",
        HOST_QUARANTINE_DIR: base + "/librairy-quarantine",
        HOST_APPDATA_DIR: base + "/librairy-appdata",
      };
      fields.forEach(function (f) {
        var key = f.getAttribute("data-key");
        if (map[key]) f.value = map[key];
      });
      render();
    });
  }

  if (copy) {
    copy.addEventListener("click", function () {
      out.select();
      try {
        document.execCommand("copy");
        copy.textContent = "Copied";
        setTimeout(function () {
          copy.textContent = "Copy .env snippet";
        }, 1500);
      } catch (e) {
        /* selection is enough if clipboard is blocked */
      }
    });
  }

  render();
})();
