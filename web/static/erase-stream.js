// Progressive enhancement for the erasure console's progress region.
//
// The page is operable without this file: the region renders the attempt identifier
// and a link to its event stream. With it, each event appends one list item to the
// polite live region, so progress is announced without moving focus.
//
// The server reads durable rows and then ends the response, so reconnection is the
// normal case rather than a failure. The browser's own EventSource reconnects on the
// retry hint the stream carries; the terminal outcome event closes the source, which
// is how the client learns the result rather than by observing a closed connection.
(function () {
  "use strict";

  var region = document.getElementById("erase-progress");
  if (!region || !window.EventSource) {
    return;
  }
  var attempt = region.getAttribute("data-attempt");
  if (!attempt) {
    return;
  }

  var source = new EventSource("/erase/" + encodeURIComponent(attempt) + "/stream");

  function announce(text) {
    var item = document.createElement("li");
    item.textContent = text;
    region.appendChild(item);
  }

  function read(event) {
    try {
      return JSON.parse(event.data);
    } catch (error) {
      return null;
    }
  }

  source.addEventListener("queued", function () {
    announce("queued: the attempt is recorded and the run has not claimed its lease");
  });

  source.addEventListener("phase", function (event) {
    var state = read(event);
    if (!state) {
      return;
    }
    announce(
      "phase " +
        state.phase +
        ": " +
        state.candidates +
        " candidates, " +
        state.residue_candidates +
        " residue candidates, " +
        state.residue_included +
        " included"
    );
  });

  source.addEventListener("outcome", function (event) {
    var state = read(event);
    if (state) {
      announce(
        "outcome " + state.status + " at phase " + state.phase +
          (state.error_detail ? ": " + state.error_detail : "")
      );
    }
    source.close();
  });
})();
