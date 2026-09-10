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

  // --- when the request does not come back -----------------------------------
  //
  // htmx does not swap an error response. That is the right default and it left
  // this application silent: press Approve while the worker holds the write
  // lock and the row stays exactly as it was, the button springs back, and
  // nothing anywhere says the approval did not happen. The page still reads
  // like the page before the press — which is the worst possible outcome, since
  // "it looks unchanged" is precisely what a *successful* toggle-back looks
  // like too.
  //
  // One handler for every failing action. `review.js` keeps its own for preview
  // panels, where the failure has a natural place to be shown; everything else
  // lands here.

  // What to say. The server sends `{"detail": "..."}` for an htmx request, and
  // that text is written for a person — every refusal in this application is a
  // sentence, not a code. Anything else falls back to what the status means.
  // A refusal detail is written for a JSON body — "that item no longer exists"
  // — and reads as a fragment when it is the only thing on screen. Capitalised
  // only when the opening word is plainly a word: `iPhone_1.jpg` keeps its own
  // shape, and so does anything that starts with a path.
  function sentence(text) {
    var opens = /^[a-z][a-z]/.test(text);
    return opens ? text.charAt(0).toUpperCase() + text.slice(1) : text;
  }

  function reason(xhr) {
    try {
      var body = JSON.parse(xhr.responseText);
      if (body && typeof body.detail === "string" && body.detail) return sentence(body.detail);
    } catch (err) {
      /* not JSON: fall through to the status */
    }
    if (!xhr.status) return "LibrAIry did not answer. Check that it is still running.";
    if (xhr.status === 403)
      // A stale CSRF token, which is what a tab left open overnight produces.
      // The request never reached the route, so nothing happened — and the fix
      // is a page load, which is a GET and safe to advise on any screen.
      return "LibrAIry did not accept that — this page has been open a while. Reload it.";
    if (xhr.status === 404) return "That is no longer there.";
    if (xhr.status === 409 || xhr.status === 422) return "That could not be done.";
    if (xhr.status >= 500) return "LibrAIry hit a problem and did not do that.";
    return "That did not happen (" + xhr.status + ").";
  }

  // Deliberately no "try again" in any of these. After an action that may have
  // touched files, a generic retry is advice nobody can give without knowing
  // what happened to the bytes — Commit and Undo answer that on their own pages,
  // from the journal, and this message's job is to stop somebody believing the
  // press worked. See `docs/ui-vocabulary.md`.
  function failed(event) {
    var trigger = event.detail && event.detail.elt;
    var said = reason((event.detail && event.detail.xhr) || {});
    // Somewhere else has already shown this one where it belongs — the preview
    // panels in `review.js` put the reason inside the panel that stayed empty.
    // It still has to be *said*: writing into a panel is not a swap, so nothing
    // announces it. So the note is conditional and the announcement is not.
    var handled = event.detail && event.detail.errorHandled;
    if (!handled && trigger && trigger.parentNode) {
      var previous = trigger.parentNode.querySelector(":scope > .action-failed");
      if (previous) previous.remove();
      var note = document.createElement("p");
      note.className = "status warn action-failed";
      note.textContent = said;
      trigger.parentNode.insertBefore(note, trigger.nextSibling);
      // Nothing was swapped, so nothing took focus away — and the element that
      // failed is where somebody wants to be. Only restore it if it was lost.
      var active = document.activeElement;
      if ((!active || active === document.body) && trigger.focus) trigger.focus();
    }
    // Said as well as shown. The live region is emptied first for the same
    // reason it is in `announce`: pressing a failing button twice is two
    // events, and a region whose text has not changed announces nothing.
    if (!region) return;
    region.textContent = "";
    window.setTimeout(function () {
      region.textContent = said;
    }, 50);
  }

  // Deferred by a tick so it does not depend on which script registered first.
  // `review.js` marks the event as handled synchronously; reading that mark from
  // a listener registered in another file is otherwise a load-order accident.
  function onError(event) {
    window.setTimeout(function () {
      failed(event);
    }, 0);
  }

  document.body.addEventListener("htmx:responseError", onError);
  document.body.addEventListener("htmx:sendError", onError);
  document.body.addEventListener("htmx:timeout", onError);
})();
