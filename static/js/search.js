/* ShuleHub search dropdown.
   - Uses a search box that already exists (a form whose action contains /search with input[name=q]).
   - If there is none, adds one to the top navigation so the feature always shows up.
   - Typing shows grouped suggestions from /search/suggest; Enter or "See all results" opens the full page.
   - Up/Down move through suggestions, Enter opens the highlighted one, Esc closes. */
(function () {
  "use strict";
  var BASE = "/search/";
  var DELAY = 220, MIN = 2;

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function findOrCreate() {
    var input = document.querySelector('form[action*="/search"] input[name="q"]');
    if (input) return input;
    var host = document.querySelector("header, .topbar, .navbar, body > nav") || document.body;
    var form = el("form", "sh-search no-print");
    form.method = "get"; form.action = BASE; form.setAttribute("role", "search");
    input = el("input"); input.type = "search"; input.name = "q"; input.placeholder = "Search…";
    input.setAttribute("aria-label", "Search ShuleHub"); input.autocomplete = "off";
    var btn = el("button", null, "Search"); btn.type = "submit";
    form.appendChild(input); form.appendChild(btn);
    host.appendChild(form);
    return input;
  }

  function init() {
    var input = findOrCreate();
    var form = input.form;
    if (!form || input.dataset.shSearch) return;
    input.dataset.shSearch = "1";
    input.setAttribute("autocomplete", "off");
    if (getComputedStyle(form).position === "static") form.style.position = "relative";

    var box = el("div", "sh-suggest"); box.hidden = true; box.setAttribute("role", "listbox");
    form.appendChild(box);

    var timer = null, seq = 0, links = [], active = -1;

    function close() { box.hidden = true; active = -1; }
    function setActive(i) {
      links.forEach(function (a) { a.classList.remove("sg-active"); });
      active = i;
      if (links[i]) { links[i].classList.add("sg-active"); links[i].scrollIntoView({ block: "nearest" }); }
    }

    function render(data, q) {
      box.textContent = ""; links = []; active = -1;
      if (!data.groups.length) {
        box.appendChild(el("div", "sg-empty", "Nothing found for \u201C" + q + "\u201D."));
      }
      data.groups.forEach(function (g) {
        box.appendChild(el("div", "sg-head", g.label));
        g.items.forEach(function (it) {
          if (!it.url) return;
          var a = el("a"); a.href = it.url; a.setAttribute("role", "option");
          var t = el("span", "sg-title", it.title);
          if (it.badge) t.appendChild(el("span", "sg-badge", it.badge));
          a.appendChild(t);
          if (it.sub) a.appendChild(el("span", "sg-sub", it.sub));
          box.appendChild(a); links.push(a);
        });
        if (g.more) box.appendChild(el("div", "sg-sub sg-empty", "More " + g.label.toLowerCase() + " on the full results page."));
      });
      var all = el("a", "sg-all", "See all results for \u201C" + q + "\u201D");
      all.href = BASE + "?q=" + encodeURIComponent(q);
      box.appendChild(all); links.push(all);
      box.hidden = false;
      place();
    }

    function place() {
      // open towards the side that has room, so it never runs off the screen
      box.style.left = "auto"; box.style.right = "0";
      var r = box.getBoundingClientRect();
      if (r.left < 8) { box.style.right = "auto"; box.style.left = "0"; }
    }

    function fetchNow() {
      var q = input.value.trim();
      if (q.length < MIN) { close(); return; }
      var mine = ++seq;
      fetch(BASE + "suggest?q=" + encodeURIComponent(q), { headers: { "Accept": "application/json" }, credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) { if (d && mine === seq) render(d, q); })
        .catch(function () { /* the full results page still works */ });
    }

    input.addEventListener("input", function () { clearTimeout(timer); timer = setTimeout(fetchNow, DELAY); });
    input.addEventListener("focus", function () { if (links.length) box.hidden = false; });
    input.addEventListener("keydown", function (e) {
      if (box.hidden) return;
      if (e.key === "ArrowDown") { e.preventDefault(); setActive(Math.min(active + 1, links.length - 1)); }
      else if (e.key === "ArrowUp") { e.preventDefault(); setActive(Math.max(active - 1, 0)); }
      else if (e.key === "Enter" && active >= 0) { e.preventDefault(); links[active].click(); }
      else if (e.key === "Escape") { close(); }
    });
    document.addEventListener("click", function (e) { if (!form.contains(e.target)) close(); });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
