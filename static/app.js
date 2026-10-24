// Category filter + live search. Everything is already in the HTML;
// this just shows and hides it.
(function () {
  "use strict";

  var chips = Array.prototype.slice.call(document.querySelectorAll("[data-filter]"));
  var items = Array.prototype.slice.call(document.querySelectorAll(".item"));
  var days = Array.prototype.slice.call(document.querySelectorAll("[data-day]"));
  var search = document.getElementById("search");
  var countEl = document.getElementById("result-count");
  var noResults = document.getElementById("no-results");
  var FILTERS = ["all", "esri", "open-source", "industry"];

  // Pre-compute lowercase search text for each item once.
  var haystacks = items.map(function (el) {
    return (el.textContent || "").replace(/\s+/g, " ").toLowerCase();
  });

  var state = { filter: "all", query: "" };

  function readHash() {
    var h = (location.hash || "").replace("#", "");
    return FILTERS.indexOf(h) >= 0 ? h : "all";
  }

  function apply() {
    var terms = state.query.toLowerCase().split(/\s+/).filter(Boolean);
    var shown = 0;

    items.forEach(function (el, i) {
      var catOk = state.filter === "all" || el.getAttribute("data-category") === state.filter;
      var textOk = terms.every(function (t) { return haystacks[i].indexOf(t) !== -1; });
      var visible = catOk && textOk;
      el.hidden = !visible;
      if (visible) shown++;
    });

    days.forEach(function (day) {
      day.hidden = !day.querySelector(".item:not([hidden])");
    });

    chips.forEach(function (chip) {
      chip.setAttribute("aria-pressed", String(chip.getAttribute("data-filter") === state.filter));
    });

    noResults.hidden = shown !== 0 || items.length === 0;
    if (state.query || state.filter !== "all") {
      countEl.textContent = shown + (shown === 1 ? " story" : " stories");
    } else {
      countEl.textContent = items.length + " stories from the last " + (document.body.getAttribute("data-max-age") || "14") + " days";
    }
  }

  chips.forEach(function (chip) {
    chip.addEventListener("click", function () {
      state.filter = chip.getAttribute("data-filter");
      // Keep the choice in the URL so it survives a reload or can be shared.
      var hash = state.filter === "all" ? "" : "#" + state.filter;
      history.replaceState(null, "", location.pathname + location.search + hash);
      apply();
    });
  });

  var timer;
  search.addEventListener("input", function () {
    clearTimeout(timer);
    timer = setTimeout(function () {
      state.query = search.value.trim();
      apply();
    }, 80);
  });

  // Press "/" to jump to the search box.
  document.addEventListener("keydown", function (e) {
    if (e.key === "/" && document.activeElement !== search) {
      e.preventDefault();
      search.focus();
    } else if (e.key === "Escape" && document.activeElement === search) {
      search.value = "";
      state.query = "";
      apply();
    }
  });

  window.addEventListener("hashchange", function () {
    state.filter = readHash();
    apply();
  });

  state.filter = readHash();
  apply();
})();
