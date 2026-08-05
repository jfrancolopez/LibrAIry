// Group the Settings page into tabs.
//
// Fourteen sections in one scroll meant hunting for anything. A section can
// appear in more than one panel (Backup and Storage are both "System" but sit
// at opposite ends of the document), so panels are matched by name, not
// position, and every panel with that name shows together.
//
// The chosen tab is remembered per browser, and a #hash still wins so existing
// deep links keep landing on the right thing.
(function () {
  var STORAGE_KEY = "librairy.settings.tab";
  var bar = document.querySelector("[data-settings-tabs]");
  if (!bar) return;

  var tabs = Array.prototype.slice.call(bar.querySelectorAll("[data-tab]"));
  var panels = Array.prototype.slice.call(document.querySelectorAll("[data-tab-panel]"));

  function show(name) {
    panels.forEach(function (panel) {
      panel.hidden = panel.dataset.tabPanel !== name;
    });
    tabs.forEach(function (tab) {
      var active = tab.dataset.tab === name;
      tab.classList.toggle("is-active", active);
      tab.setAttribute("aria-selected", active ? "true" : "false");
    });
    try {
      localStorage.setItem(STORAGE_KEY, name);
    } catch (err) {
      // Private browsing. Losing the preference is not worth breaking tabs.
    }
  }

  function tabForHash() {
    if (!window.location.hash) return "";
    var target = document.querySelector(window.location.hash);
    var panel = target && target.closest("[data-tab-panel]");
    return panel ? panel.dataset.tabPanel : "";
  }

  function known(name) {
    return tabs.some(function (tab) {
      return tab.dataset.tab === name;
    });
  }

  bar.addEventListener("click", function (event) {
    var tab = event.target.closest("[data-tab]");
    if (tab) show(tab.dataset.tab);
  });

  // A validation error is useless on a hidden panel, so it takes priority.
  var errored = document.querySelector("#settings-error, #settings-result");
  var erroredPanel = errored && errored.closest("[data-tab-panel]");
  var stored = "";
  try {
    stored = localStorage.getItem(STORAGE_KEY) || "";
  } catch (err) {
    stored = "";
  }

  var initial =
    (erroredPanel && erroredPanel.dataset.tabPanel) ||
    tabForHash() ||
    (known(stored) ? stored : "") ||
    tabs[0].dataset.tab;
  show(initial);
})();
