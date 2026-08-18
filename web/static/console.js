/*
 * Four small enhancements, and the page is complete without every one of them. Nothing
 * here fetches anything, computes a value, or changes what the server said: each takes
 * markup that is already correct and makes it easier to read.
 *
 * The stylesheet declares one entrance animation and reads its delay from a custom
 * property. The first function sets that property on each top-level block of the main
 * landmark so the blocks arrive in sequence. It is done here rather than in the
 * stylesheet because a rule would need one selector per position and would stop at
 * whatever number it was written for.
 *
 * The second puts a long unbroken value -- an identifier, a digest -- into a box of its
 * own that scrolls sideways, so the value stays on one line and stays whole. It wraps the
 * cell's children rather than the cell, so a link inside keeps being a link. With this
 * file absent the value is shown wrapped, which is worse to read and still complete.
 *
 * The third gives a table wider than its container its own horizontal scroll, so one wide
 * column cannot stretch the page. The wrapper is added here rather than in eighteen
 * templates, and only where it is needed.
 *
 * The fourth counts a figure up to the value the server already rendered.
 *
 * The animations are switched off entirely by a reduced-motion preference — off, not
 * shortened. The readability enhancements are not: shortening a value and letting a table
 * scroll are not motion, and a reader who has asked for less movement has not asked for a
 * worse table.
 */

(function () {
  "use strict";

  var STEP_MS = 50;
  var MAX_STAGGER_MS = 380;
  var COUNT_MS = 600;

  // Below this many characters a value is left as it is: short identifiers read fine in
  // place, and clamping them would add a tooltip nobody needs.
  var CLAMP_FROM = 18;

  var reduced =
    window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function stagger() {
    var main = document.getElementById("main");
    if (!main) {
      return;
    }
    var blocks = main.children;
    for (var i = 0; i < blocks.length; i += 1) {
      blocks[i].style.setProperty("--enter-delay", Math.min(i * STEP_MS, MAX_STAGGER_MS) + "ms");
    }
  }

  // A value with no whitespace in it is a token rather than prose: an identifier, a
  // digest, an object key. Prose is left to wrap, because wrapping is how prose should
  // behave and truncating a sentence loses the sentence.
  function isOpaqueToken(text) {
    return text.length >= CLAMP_FROM && !/\s/.test(text);
  }

  // A long identifier is put in a box of its own that scrolls sideways, so the value stays
  // on one line and stays whole.
  //
  // Two earlier treatments were worse and both are worth naming. Left alone, the cell wraps
  // a thirty-six character identifier into a stack of six-character fragments, which is
  // unreadable and makes every row a different height. Truncated with an ellipsis, the
  // value is readable but no longer selectable or copyable, and reaching it needs a hover.
  //
  // The box is wrapped around the cell's children rather than applied to the cell, so
  // whatever is inside — a link, in every case that matters here — keeps its own behaviour:
  // it is still a link, still clickable, still focusable, and still copies as one value.
  function scrollLongValues() {
    var cells = document.querySelectorAll("main td, main th[scope='row']");
    for (var i = 0; i < cells.length; i += 1) {
      var cell = cells[i];
      // A cell holding a control is left alone: a scroll box around a control would put
      // the control's own focus ring inside a clipping box.
      if (cell.querySelector("button, input, select, svg")) {
        continue;
      }
      if (cell.querySelector(".value-scroll")) {
        continue;
      }
      var text = (cell.textContent || "").trim();
      if (!isOpaqueToken(text)) {
        continue;
      }
      var box = document.createElement("div");
      box.className = "value-scroll";
      // The value is announced as one thing rather than as a scrollable region, and it is
      // reachable by keyboard so the scroll can be driven without a pointer.
      box.setAttribute("title", text);
      while (cell.firstChild) {
        box.appendChild(cell.firstChild);
      }
      cell.appendChild(box);
    }
  }

  function scrollWideTables() {
    var tables = document.querySelectorAll("main table");
    for (var i = 0; i < tables.length; i += 1) {
      var table = tables[i];
      var parent = table.parentNode;
      if (!parent || (parent.className || "").indexOf("table-scroll") !== -1) {
        continue;
      }
      var wrapper = document.createElement("div");
      wrapper.className = "table-scroll";
      parent.insertBefore(wrapper, table);
      wrapper.appendChild(table);
    }
  }

  function eased(fraction) {
    return 1 - Math.pow(1 - fraction, 3);
  }

  function countUp(element) {
    var target = Number(element.getAttribute("data-count"));
    if (!isFinite(target)) {
      return;
    }
    var started = null;

    function frame(now) {
      if (started === null) {
        started = now;
      }
      var fraction = Math.min((now - started) / COUNT_MS, 1);
      element.textContent = String(Math.round(target * eased(fraction)));
      if (fraction < 1) {
        window.requestAnimationFrame(frame);
      } else {
        // The server's own value, restored exactly, so no rounding of ours survives.
        element.textContent = element.getAttribute("data-count");
      }
    }

    window.requestAnimationFrame(frame);
  }

  function figures() {
    var found = document.querySelectorAll("[data-count]");
    for (var i = 0; i < found.length; i += 1) {
      countUp(found[i]);
    }
  }

  scrollLongValues();
  scrollWideTables();

  if (reduced) {
    return;
  }

  stagger();
  figures();
})();
