// Select-all for the Review table, plus a live count of what is selected.
//
// The bulk buttons ("Approve Selected") were only reachable by ticking every
// row by hand, and the only shortcut was "select all matching current filter",
// which is a much bigger and scarier commitment than "these ones, here".
//
// Delegated from the document so it survives htmx swapping the list.
(function () {
  // Two selections on one page, and they must never touch. The inbox queue
  // selects proposals; Library Audit selects findings about files you already
  // own, and an inbox bulk action must be structurally incapable of receiving
  // one. So each scope names its own field, its own container and its own
  // toolbar, and every query below is scoped to one of them. One
  // implementation, two configurations — not two copies to drift apart.
  var SCOPES = [
    {
      field: "proposal_id",
      root: "review-list",
      group: ".review-group",
      selectAll: "select-all",
      count: "[data-selected-count]",
      needs: "[data-needs-selection]",
      only: "[data-selection-only]",
      clear: null,
      eligibility: null
    },
    {
      field: "finding_id",
      root: "library-audit",
      group: ".audit-group",
      selectAll: "audit-select-all",
      count: "[data-audit-selected-count]",
      needs: "[data-audit-needs-selection]",
      only: "[data-audit-selection-only]",
      clear: ".audit-clear",
      eligibility: "[data-audit-eligibility]"
    }
  ];

  function scopeFor(node) {
    for (var i = 0; i < SCOPES.length; i++) {
      var root = document.getElementById(SCOPES[i].root);
      if (root && root.contains(node)) return SCOPES[i];
      if (node.name === SCOPES[i].field) return SCOPES[i];
      if (node.classList && node.classList.contains(SCOPES[i].selectAll)) return SCOPES[i];
    }
    return null;
  }

  // A select-all in a group heading covers that group; the one in the toolbar
  // sits outside every group and so covers that scope's whole list.
  function rowsUnder(header, scope) {
    var container = header.closest(scope.group) || document.getElementById(scope.root);
    return container
      ? Array.prototype.slice.call(
          container.querySelectorAll('input[name="' + scope.field + '"]')
        )
      : [];
  }

  function allBoxes(scope) {
    var root = document.getElementById(scope.root);
    return root
      ? Array.prototype.slice.call(root.querySelectorAll('input[name="' + scope.field + '"]'))
      : [];
  }

  function refreshCount(scope) {
    var label = document.querySelector(scope.count);
    if (!label) return;
    var chosen = allBoxes(scope).filter(function (box) {
      return box.checked;
    });
    var selected = chosen.length;
    label.textContent = selected ? selected + " selected" : "";
    document.querySelectorAll(scope.needs).forEach(function (button) {
      button.disabled = selected === 0;
    });
    // Five disabled buttons held a permanent line in a sticky bar for a
    // selection that does not exist yet. They appear when they can be used.
    document.querySelectorAll(scope.only).forEach(function (group) {
      group.hidden = selected === 0;
    });
    if (scope.eligibility) refreshEligibility(scope, chosen);
  }

  // Mixed selections are explained before the button is pressed, not after.
  // Selecting one correction and two observations must not look as though
  // three things are about to be accepted.
  function refreshEligibility(scope, chosen) {
    var note = document.querySelector(scope.eligibility);
    var eligible = chosen.filter(function (box) {
      return box.dataset.auditEligible === "1";
    }).length;
    document.querySelectorAll("[data-audit-needs-eligible]").forEach(function (button) {
      button.disabled = eligible === 0;
      button.textContent =
        eligible && eligible !== chosen.length
          ? "Accept corrections (" + eligible + " eligible)"
          : "Accept corrections";
    });
    if (!note) return;
    var rest = chosen.length - eligible;
    note.textContent = rest
      ? rest + " of " + chosen.length + " cannot be accepted — observations, or changed since the audit"
      : "";
  }

  function syncHeaders(scope) {
    document.querySelectorAll("." + scope.selectAll).forEach(function (header) {
      var boxes = rowsUnder(header, scope);
      var checked = boxes.filter(function (box) {
        return box.checked;
      }).length;
      header.checked = boxes.length > 0 && checked === boxes.length;
      // The in-between state, so a partial selection does not read as "none".
      header.indeterminate = checked > 0 && checked < boxes.length;
    });
  }

  function refreshAll() {
    SCOPES.forEach(function (scope) {
      syncHeaders(scope);
      refreshCount(scope);
    });
  }

  document.addEventListener("change", function (event) {
    var target = event.target;
    var scope = scopeFor(target);
    if (!scope) return;
    if (target.classList && target.classList.contains(scope.selectAll)) {
      var checked = target.checked;
      rowsUnder(target, scope).forEach(function (box) {
        box.checked = checked;
      });
    } else if (target.name !== scope.field) {
      return;
    }
    syncHeaders(scope);
    refreshCount(scope);
  });

  // Clearing one selection leaves the other exactly as it was.
  document.addEventListener("click", function (event) {
    var button = event.target.closest(".audit-clear");
    if (!button) return;
    var scope = SCOPES[1];
    allBoxes(scope).forEach(function (box) {
      box.checked = false;
    });
    document.querySelectorAll("." + scope.selectAll).forEach(function (header) {
      header.checked = false;
      header.indeterminate = false;
    });
    refreshCount(scope);
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
    if (
      !target ||
      !(
        target.classList.contains("proposal-preview") ||
        target.classList.contains("audit-preview")
      )
    )
      return;
    var reason =
      event.detail.xhr.status === 404
        ? "Preview unavailable — the file has moved or been removed."
        : "Preview failed (" + event.detail.xhr.status + ").";
    target.innerHTML = '<p class="muted preview-failed"></p>';
    target.firstChild.textContent = reason;
  });

  document.body.addEventListener("htmx:afterSwap", refreshAll);

  refreshAll();
})();
