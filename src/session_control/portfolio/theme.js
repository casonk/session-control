/* Generated from themes/themes.toml by scripts/render_portfolio_theme.py; do not edit. */
(function () {
  var THEMES = {
  "midnight": {
    "label": "Midnight",
    "scheme": "dark",
    "icon": "\ud83c\udf19",
    "swatch": [
      "#0f1117",
      "#5b7fff",
      "#e2e4f0"
    ]
  },
  "daylight": {
    "label": "Daylight",
    "scheme": "light",
    "icon": "\u2600\ufe0f",
    "swatch": [
      "#f4f5fa",
      "#3b5bdb",
      "#1c2033"
    ]
  },
  "ember": {
    "label": "Ember",
    "scheme": "dark",
    "icon": "\ud83d\udd25",
    "swatch": [
      "#1a1614",
      "#e8913a",
      "#ece3da"
    ]
  },
  "contrast-dark": {
    "label": "High contrast dark",
    "scheme": "dark",
    "icon": "\u2b1b",
    "swatch": [
      "#000000",
      "#ffd400",
      "#ffffff"
    ]
  },
  "contrast-light": {
    "label": "High contrast white",
    "scheme": "light",
    "icon": "\u2b1c",
    "swatch": [
      "#ffffff",
      "#0000cc",
      "#000000"
    ]
  },
  "liberty": {
    "label": "Liberty",
    "scheme": "light",
    "icon": "\ud83c\uddfa\ud83c\uddf8",
    "swatch": [
      "#f2f5fb",
      "#0a3161",
      "#0a2342"
    ]
  },
  "hulk": {
    "label": "Hulk",
    "scheme": "dark",
    "icon": "\ud83d\udc9a",
    "swatch": [
      "#140a1f",
      "#6fe03a",
      "#f0e8ff"
    ]
  },
  "neon": {
    "label": "Neon",
    "scheme": "dark",
    "icon": "\u26a1",
    "swatch": [
      "#07070f",
      "#00f0ff",
      "#f4f4ff"
    ]
  }
};
  var KEY = "portfolio-theme";
  var ALIASES = {"contrast": "contrast-dark"};
  function known(name) { name = ALIASES[name] || name; return THEMES[name] ? name : null; }
  var DARK = "midnight", LIGHT = "daylight";
  var root = document.documentElement;
  function saved() {
    try { return localStorage.getItem(KEY); } catch (e) { return null; }
  }
  function pick() {
    var asked = new URLSearchParams(location.search).get("theme");
    if (known(asked)) return known(asked);
    var kept = saved();
    if (known(kept)) return known(kept);
    return window.matchMedia && matchMedia("(prefers-color-scheme: light)").matches ? LIGHT : DARK;
  }
  var ORDER = Object.keys(THEMES);
  function current() { return root.getAttribute("data-theme"); }
  function following(name) { return ORDER[(ORDER.indexOf(name) + 1) % ORDER.length]; }
  function paintCycle(el, name) {
    var theme = THEMES[name], target = THEMES[following(name)];
    var icon = el.querySelector("[data-theme-icon]"), label = el.querySelector("[data-theme-name]");
    if (icon || label) {
      if (icon) icon.textContent = theme.icon;
      if (label) label.textContent = theme.label;
    } else {
      el.textContent = theme.icon + " " + theme.label;
    }
    var text = "Theme: " + theme.label + ". Switch to " + target.label + ".";
    el.setAttribute("aria-label", text);
    el.setAttribute("title", text);
  }
  function apply(name) {
    root.setAttribute("data-theme", name);
    document.querySelectorAll("[data-theme-switcher]").forEach(function (el) { el.value = name; });
    document.querySelectorAll("[data-theme-cycle]").forEach(function (el) { paintCycle(el, name); });
    document.querySelectorAll("[data-theme-menu]").forEach(function (menu) { paintMenu(menu, name); });
  }
  function paintMenu(menu, name) {
    menu.querySelectorAll("[data-theme-choice]").forEach(function (item) {
      item.setAttribute("aria-checked", String(item.getAttribute("data-theme-choice") === name));
    });
  }
  function fill(menu) {
    if (menu.pfFilled) return;
    menu.pfFilled = true;
    menu.setAttribute("role", "menu");
    menu.setAttribute("aria-label", "Themes");
    ORDER.forEach(function (name) {
      var theme = THEMES[name], item = document.createElement("button");
      item.type = "button";
      item.setAttribute("role", "menuitemradio");
      item.setAttribute("data-theme-choice", name);
      var swatch = document.createElement("span");
      swatch.className = "pf-swatch";
      swatch.style.background = "linear-gradient(90deg," + theme.swatch[0] + " 0 34%," +
        theme.swatch[1] + " 34% 67%," + theme.swatch[2] + " 67%)";
      var label = document.createElement("span");
      label.textContent = theme.icon + " " + theme.label;
      var kind = document.createElement("small");
      kind.textContent = theme.scheme;
      item.appendChild(swatch); item.appendChild(label); item.appendChild(kind);
      item.addEventListener("click", function () {
        set(name);
        var open = menu.closest && menu.closest("details");
        if (open) open.open = false;
      });
      menu.appendChild(item);
    });
    paintMenu(menu, current() || pick());
  }
  function set(name) {
    name = known(name);
    if (!name) return;
    try { localStorage.setItem(KEY, name); } catch (e) {}
    apply(name);
  }
  function cycle() { set(following(current() || pick())); }
  function bind(el) {
    if (el.pfBound) return;
    el.pfBound = true;
    el.addEventListener("click", cycle);
    paintCycle(el, current() || pick());
  }
  apply(pick());
  window.addEventListener("storage", function (event) {
    if (event.key === KEY && known(event.newValue)) apply(known(event.newValue));
  });
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-theme-cycle]").forEach(bind);
    document.querySelectorAll("[data-theme-menu]").forEach(fill);
    document.querySelectorAll("[data-theme-switcher]").forEach(function (el) {
      if (el.tagName === "SELECT" && !el.options.length) {
        Object.keys(THEMES).forEach(function (name) {
          var option = document.createElement("option");
          option.value = name;
          option.textContent = THEMES[name].label;
          el.appendChild(option);
        });
      }
      el.setAttribute("aria-label", "Theme");
      el.addEventListener("change", function () { set(el.value); });
    });
    apply(root.getAttribute("data-theme") || pick());
  });
  window.PortfolioTheme = { themes: THEMES, set: set, cycle: cycle, bind: bind, fill: fill, current: current };
})();
