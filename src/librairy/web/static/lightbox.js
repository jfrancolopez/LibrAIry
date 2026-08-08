// Open a preview full screen.
//
// A 320px thumbnail answers "is there a picture here"; it does not answer "is
// this the right picture". The row keeps the thumbnail and this shows the
// larger render the server already knows how to produce.
//
// Built on a real <dialog>, so Escape, focus trapping and restoring focus to
// the button you came from are the browser's job rather than three more
// listeners to get subtly wrong. Everything it displays comes from data
// attributes the server wrote — URLs it generated, never a filesystem path.
(function () {
  var dialog = document.getElementById("lightbox");
  if (!dialog || typeof dialog.showModal !== "function") return;

  var stage = dialog.querySelector("[data-lightbox-stage]");
  var titleEl = dialog.querySelector("[data-lightbox-title]");
  var factsEl = dialog.querySelector("[data-lightbox-facts]");
  var zoom = dialog.querySelector("[data-lightbox-zoom]");

  function open(source) {
    var data = source.dataset;
    titleEl.textContent = data.lightboxTitle || "Preview";
    factsEl.textContent = data.lightboxFacts || "";
    stage.classList.remove("is-actual");
    stage.replaceChildren();
    zoom.hidden = true;

    if (data.lightboxVideo) {
      stage.appendChild(video(data));
    } else if (data.lightboxImage) {
      stage.appendChild(image(data));
      zoom.hidden = false;
      zoom.textContent = "Actual size";
    }
    dialog.showModal();
  }

  function image(data) {
    var img = document.createElement("img");
    img.className = "lightbox-image";
    img.alt = "Full screen preview of " + (data.lightboxTitle || "this file");
    img.addEventListener("load", function () {
      // The real pixel dimensions, free, once the browser has the file. No
      // reason to make the server measure something the client can read.
      var size = img.naturalWidth + " × " + img.naturalHeight;
      factsEl.textContent = data.lightboxFacts
        ? size + " · " + data.lightboxFacts
        : size;
    });
    img.src = data.lightboxImage;
    return img;
  }

  function video(data) {
    var player = document.createElement("video");
    player.className = "lightbox-video";
    player.controls = true;
    player.playsInline = true;
    // No autoplay: a page of proposals is not a place for sound to start on
    // its own. The poster is the thumbnail the row already loaded.
    player.preload = "metadata";
    if (data.lightboxImage) player.poster = data.lightboxImage;
    var source = document.createElement("source");
    source.src = data.lightboxVideo;
    if (data.lightboxType) source.type = data.lightboxType;
    player.appendChild(source);
    return player;
  }

  function toggleActualSize() {
    var actual = stage.classList.toggle("is-actual");
    zoom.textContent = actual ? "Fit to window" : "Actual size";
  }

  document.addEventListener("click", function (event) {
    var trigger = event.target.closest("[data-lightbox]");
    if (trigger) {
      open(trigger);
      return;
    }
    // The picture in the row is its own expand control. The button beside it
    // carries the data, so the image only has to find it.
    var expandable = event.target.closest("img.is-expandable");
    if (expandable) {
      var button = expandable
        .closest(".preview-media")
        .querySelector("[data-lightbox]");
      if (button) open(button);
    }
  });

  // Tearing down is its own function called from every exit, rather than a
  // `close` listener, because the `close` event cannot be relied on: measured
  // in this project's own browser, showModal() then close() opens and closes
  // the dialog correctly and fires no `close` at all. Hanging the teardown off
  // that event left a video playing to nobody behind a shut viewer — which is
  // the one thing a media modal must never do. The listeners below stay as a
  // backstop for engines that do fire them; dismiss() is idempotent.
  function dismiss() {
    var player = stage.querySelector("video");
    if (player) player.pause();
    stage.replaceChildren();
    stage.classList.remove("is-actual");
    if (dialog.open) dialog.close();
  }

  dialog.addEventListener("click", function (event) {
    if (event.target.closest("[data-lightbox-close]")) {
      dismiss();
      return;
    }
    if (event.target.closest("[data-lightbox-zoom]")) {
      toggleActualSize();
      return;
    }
    // The backdrop, and the empty space around the media. Never the media:
    // closing the viewer because somebody clicked the photograph they opened
    // it to look at is the classic way to make a lightbox infuriating.
    if (event.target === dialog || event.target === stage) dismiss();
  });

  // Escape closes a <dialog> natively, which would skip the teardown above.
  // Handling the key ourselves keeps one exit path for every way out.
  dialog.addEventListener("keydown", function (event) {
    if (event.key === "Escape") dismiss();
  });

  stage.addEventListener("dblclick", function (event) {
    if (event.target.tagName === "IMG") toggleActualSize();
  });

  dialog.addEventListener("cancel", dismiss);
  dialog.addEventListener("close", dismiss);
})();
