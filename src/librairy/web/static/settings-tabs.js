// Group the Settings page into tabs.
//
// Fourteen sections in one scroll meant hunting for anything. A section can
// appear in more than one panel (Backup and Storage are both "System" but sit
// at opposite ends of the document), so panels are matched by name, not
// position, and every panel with that name shows together.
//
// Opening Settings always lands on the first tab. It used to remember the last
// one you were on, which sounds helpful and is not: you come to Settings to
// change one thing, and weeks later it opens on a panel you have no memory of
// choosing. A #hash still wins, so deep links keep working.
(function () {
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
  }

  function tabForHash() {
    if (!window.location.hash) return "";
    var target = document.querySelector(window.location.hash);
    var panel = target && target.closest("[data-tab-panel]");
    return panel ? panel.dataset.tabPanel : "";
  }

  bar.addEventListener("click", function (event) {
    var tab = event.target.closest("[data-tab]");
    if (tab) show(tab.dataset.tab);
  });

  // A validation error is useless on a hidden panel, so it takes priority.
  var errored = document.querySelector("#settings-error, #settings-result");
  var erroredPanel = errored && errored.closest("[data-tab-panel]");

  show(
    (erroredPanel && erroredPanel.dataset.tabPanel) ||
      tabForHash() ||
      tabs[0].dataset.tab
  );
})();
