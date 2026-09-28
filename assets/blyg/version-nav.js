/*
 * The version stepper: hugo-blyg's one script, and a progressive
 * enhancement only. Ported from the blygger reference client's in-situ
 * pinned-version carousel (worker/src/pages.ts, VERSION_NAV_SCRIPT), with
 * its class names (css-contract.md §2.1): .vnav, .vstep, .vlatest, .vextra,
 * article.showing-pin. It adds no styles.
 *
 * On an item whose version line carries data-item, it adds ‹ › buttons that
 * step the item between its pinned versions and its live one, oldest to
 * newest with the live version last, swapping the body (div.item-content)
 * and the version's note (p.version-note) in place; "open this version ↗"
 * and "back to latest" while a pin is shown; and a plain left click on a
 * pin citation shows that pin here instead of leaving the page.
 *
 * What it keeps:
 *
 * 1. Nothing depends on it. The server-rendered version line's pin
 *    citations are real links to real pinned pages, so with JavaScript off,
 *    or if this throws, every pinned version is still one click away.
 * 2. It never invents a version (§8.4). The versions it steps through are
 *    the live one plus data-pins, nothing else, and the only thing it
 *    fetches is items/{id}/v{n}.json, which exists exactly for the pins.
 *    Pinned pages are found from the citations' own hrefs, so a pin taken
 *    before a kind change still opens under f/ or t/ as it was.
 * 3. It doesn't run where it shouldn't: a pinned page's version line and a
 *    withdrawn item's carry no data-item, and a card without a body
 *    (div.item-content) is left alone.
 */
(function () {
  "use strict";
  var lines = document.querySelectorAll(".version-line[data-item]");
  if (!lines.length || !window.fetch) return;

  Array.prototype.forEach.call(lines, function (line) {
    var article = line.closest("article");
    var content = article && article.querySelector(".item-content");
    var label = line.querySelector(".vlabel");
    if (!content || !label) return;

    var id = line.dataset.item;
    var mount = line.dataset.mount || "";
    var live = Number(line.dataset.live);
    var pins = (line.dataset.pins || "").split(",").filter(Boolean).map(Number);

    // Each pin's own page, from the citation that links it.
    var pages = {};
    Array.prototype.forEach.call(line.querySelectorAll(".pins a"), function (a) {
      var v = Number((a.textContent || "").replace(/[^0-9]/g, ""));
      if (pins.indexOf(v) !== -1) pages[v] = a.href;
    });

    // Live plus each pin, once: a pin of the live version is the same bytes
    // already on the page, so it's one position, not two.
    var versions = pins.slice();
    if (versions.indexOf(live) === -1) versions.push(live);
    versions.sort(function (a, b) { return a - b; });
    if (versions.length < 2) return;

    // The note belongs to its version, so it travels with the body.
    var noteEl = article.querySelector(".version-note");
    var cache = {};
    cache[live] = { html: content.innerHTML, note: noteEl ? noteEl.textContent : "" };
    var at = versions.indexOf(live);

    function setNote(text) {
      if (!text) { if (noteEl) noteEl.hidden = true; return; }
      if (!noteEl) {
        noteEl = document.createElement("p");
        noteEl.className = "version-note";
        line.parentNode.insertBefore(noteEl, line.nextSibling);
      }
      noteEl.hidden = false;
      noteEl.textContent = text;
    }

    var nav = document.createElement("span");
    nav.className = "vnav";
    nav.innerHTML =
      '<button type="button" class="vstep" data-step="-1" title="older version" aria-label="older version">‹</button> ' +
      '<button type="button" class="vstep" data-step="1" title="newer version" aria-label="newer version">›</button> ';
    label.parentNode.insertBefore(nav, label);

    var extra = document.createElement("span");
    extra.className = "vextra";
    line.appendChild(extra);

    function render() {
      var v = versions[at];
      var isLive = v === live;
      // "frozen" in the text, so it reads without a stylesheet.
      label.textContent = "v" + v + (isLive ? "" : " · frozen");
      setNote(cache[v] ? cache[v].note : "");
      article.classList.toggle("showing-pin", !isLive);
      nav.querySelector('[data-step="-1"]').disabled = at === 0;
      nav.querySelector('[data-step="1"]').disabled = at === versions.length - 1;
      extra.textContent = "";
      if (!isLive) {
        extra.appendChild(document.createTextNode(" · "));
        var open = document.createElement("a");
        open.href = pages[v];
        open.textContent = "open this version ↗";
        extra.appendChild(open);
        extra.appendChild(document.createTextNode(" · "));
        var back = document.createElement("button");
        back.type = "button";
        back.className = "vstep vlatest";
        back.textContent = "back to latest";
        extra.appendChild(back);
      }
    }

    function show(v) {
      if (cache[v] !== undefined) {
        at = versions.indexOf(v);
        content.innerHTML = cache[v].html;
        render();
        return;
      }
      content.setAttribute("aria-busy", "true");
      fetch(mount + "/items/" + id + "/v" + v + ".json")
        .then(function (r) { if (!r.ok) throw new Error(String(r.status)); return r.json(); })
        .then(function (data) {
          // The quotes are how the page writes a note (view/version-note.html).
          cache[v] = { html: data.content_html || "", note: data.note ? "“" + data.note + "”" : "" };
          at = versions.indexOf(v);
          content.innerHTML = cache[v].html;
          content.removeAttribute("aria-busy");
          render();
        })
        .catch(function () {
          // Fall back to what always works: the pinned page itself.
          content.removeAttribute("aria-busy");
          location.href = pages[v];
        });
    }

    line.addEventListener("click", function (e) {
      var step = e.target.closest(".vstep");
      if (step) {
        e.preventDefault();
        if (step.classList.contains("vlatest")) { show(live); return; }
        var next = at + Number(step.dataset.step);
        if (next >= 0 && next < versions.length) show(versions[next]);
        return;
      }
      // A pin citation shows that version here. Its href stays on the link,
      // so middle-click, modified clicks and no-JS all still reach the page.
      var pin = e.target.closest(".pins a");
      if (!pin || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
      var v = Number((pin.textContent || "").replace(/[^0-9]/g, ""));
      if (versions.indexOf(v) === -1) return;
      e.preventDefault();
      show(v);
    });

    render();
  });
})();
