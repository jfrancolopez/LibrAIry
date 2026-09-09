// What just happened, for somebody who cannot see it happen.
//
// Most of this application answers an action with a whole new page, and a
// message in that page is read in its ordinary place. htmx does not: it swaps a
// fragment into a page that stays put, so "12 files approved" appears in
// silence — the words are on screen and nothing says them. And the element that
// was focused when the button was pressed is often *inside* the fragment that
// was just replaced, so focus falls back to the top of the document and the
// next Tab starts from the site header.
//
// Both are the same event, so both are handled here.
(function () {
  var region = document.getElementById("announcer");

  function announce(target) {
    if (!region || !target || !target.querySelector) return;
    var found = target.querySelector("[data-announce]");
    if (!found) return;
    var said = found.textContent.replace(/\s+/g, " ").trim();
    if (!said) return;
    // The same words twice — a second batch approved, the same count — is a
    // second event and has to read as one, so the region is emptied first.
    region.textContent = "";
    window.setTimeout(function () {
      region.textContent = said;
    }, 50);
  }

  function keepFocus(target) {
    // Only when it was actually lost. htmx restores focus itself when the
    // focused element comes back with the same id, and taking focus away from
    // somebody who still has it is worse than the problem being fixed.
    var active = document.activeElement;
    if (active && active !== document.body && active !== document.documentElement) return;
    if (!target || !target.focus) return;
    target.setAttribute("tabindex", "-1");
    target.focus({ preventScroll: true });
  }

  document.addEventListener("htmx:afterSwap", function (event) {
    announce(event.target);
    // A GET is the page refreshing itself — the Dashboard polls every five
    // seconds — and moving focus on one of those would yank the cursor out of
    // whatever somebody was doing, twelve times a minute. Only a swap that
    // answers a press gets to move anything.
    var config = (event.detail && event.detail.requestConfig) || {};
    if (String(config.verb || "get").toLowerCase() === "get") return;
    keepFocus(event.target);
  });
})();
