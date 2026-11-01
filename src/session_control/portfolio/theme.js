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
  var GLOBAL = null, GLOBAL_URL = "/_portfolio/global";
  var scope = "site", note = "";
  var root = document.documentElement;
  function saved() {
    try { return localStorage.getItem(KEY); } catch (e) { return null; }
  }
  function pick() {
    var asked = new URLSearchParams(location.search).get("theme");
    if (known(asked)) return known(asked);
    var kept = saved();
    if (known(kept)) return known(kept);
    if (known(GLOBAL)) return known(GLOBAL);
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
    [["site", "This site"], ["all", "Everywhere"]].forEach(function (pair) {
      var chooser = document.createElement("button");
      chooser.type = "button";
      chooser.setAttribute("role", "menuitemradio");
      chooser.setAttribute("data-theme-scope", pair[0]);
      chooser.textContent = pair[1];
      chooser.addEventListener("click", function (event) {
        event.stopPropagation();
        scope = pair[0];
        paintScope();
      });
      menu.appendChild(chooser);
    });
    var follow = document.createElement("button");
    follow.type = "button";
    follow.setAttribute("data-theme-follow", "");
    follow.addEventListener("click", function () { useGlobal(); });
    menu.appendChild(follow);
    var status = document.createElement("small");
    status.setAttribute("data-theme-note", "");
    status.textContent = note;
    menu.appendChild(status);
    paintScope();
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
        set(name, scope);
        var open = menu.closest && menu.closest("details");
        if (open) open.open = false;
      });
      menu.appendChild(item);
    });
    paintMenu(menu, current() || pick());
  }
  function say(text) {
    note = text;
    document.querySelectorAll("[data-theme-note]").forEach(function (el) { el.textContent = text; });
  }
  function overridden() { return !!known(saved()); }
  function paintScope() {
    document.querySelectorAll("[data-theme-scope]").forEach(function (el) {
      el.setAttribute("aria-checked", String(el.getAttribute("data-theme-scope") === scope));
    });
    document.querySelectorAll("[data-theme-follow]").forEach(function (el) {
      el.hidden = !overridden() || !known(GLOBAL);
      if (known(GLOBAL)) el.textContent = "Follow the global theme (" + THEMES[known(GLOBAL)].label + ")";
    });
  }
  function set(name, where) {
    name = known(name);
    if (!name) return;
    if ((where || "site") === "all") return setGlobal(name);
    try { localStorage.setItem(KEY, name); } catch (e) {}
    apply(name);
    say("");
    paintScope();
  }
  function setGlobal(name) {
    name = known(name);
    if (!name) return;
    fetch(GLOBAL_URL, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Portfolio-Global": "1" },
      body: JSON.stringify({ theme: name })
    }).then(function (response) {
      if (!response.ok) throw new Error(String(response.status));
      try { localStorage.removeItem(KEY); } catch (e) {}
      GLOBAL = name;
      apply(name);
      say("");
      paintScope();
    }).catch(function () {
      say("The global theme cannot be changed from this site; set for this site only.");
      try { localStorage.setItem(KEY, name); } catch (e) {}
      apply(name);
      paintScope();
    });
  }
  function useGlobal() {
    try { localStorage.removeItem(KEY); } catch (e) {}
    apply(pick());
    say("");
    paintScope();
  }
  function cycle() { set(following(current() || pick())); }
  function bind(el) {
    if (el.pfBound) return;
    el.pfBound = true;
    el.addEventListener("click", cycle);
    paintCycle(el, current() || pick());
  }
  var TYPE = {"font": "system", "size": "medium"};
  var FACES = {"system": "System", "serif": "Serif", "mono": "Mono"};
  var SIZES = {"tiny": "Tiny", "small": "Small", "medium": "Medium", "large": "Large"};
  var FONT_KEY = "portfolio-font", SIZE_KEY = "portfolio-size";
  var LEGACY_SIZE = { "14": "medium", "16": "large", "18": "large", "20": "large" };
  function knownFace(name) { return Object.prototype.hasOwnProperty.call(FACES, name) ? name : null; }
  function knownSize(value) {
    if (value == null) return null;
    if (Object.prototype.hasOwnProperty.call(SIZES, value)) return value;
    return LEGACY_SIZE[String(value)] || null;
  }
  function stored(key) {
    try { return localStorage.getItem(key); } catch (e) { return null; }
  }
  function pickType() {
    var params = new URLSearchParams(location.search);
    return {
      font: knownFace(params.get("font")) || knownFace(stored(FONT_KEY)) || TYPE.font,
      size: knownSize(params.get("size")) || knownSize(stored(SIZE_KEY)) || knownSize(TYPE.size)
    };
  }
  function nextName(map, now) {
    var names = Object.keys(map), index = names.indexOf(now);
    return names[(index + 1) % names.length];
  }
  function applyType(choice) {
    root.setAttribute("data-font", choice.font);
    root.setAttribute("data-size", choice.size);
    document.querySelectorAll("[data-type-font]").forEach(function (el) { el.textContent = FACES[choice.font]; });
    document.querySelectorAll("[data-type-size]").forEach(function (el) { el.textContent = SIZES[choice.size]; });
    document.querySelectorAll("[data-type-cycle]").forEach(function (el) {
      var size = el.getAttribute("data-type-cycle") === "size";
      var map = size ? SIZES : FACES, now = size ? choice.size : choice.font;
      var text = (size ? "Size: " : "Font: ") + map[now] + ". Switch to " + map[nextName(map, now)] + ".";
      el.setAttribute("title", text);
      el.setAttribute("aria-label", text);
    });
    document.querySelectorAll("[data-type-menu]").forEach(function (menu) { paintType(menu, choice); });
  }
  function paintType(menu, choice) {
    menu.querySelectorAll("[data-type-choice]").forEach(function (item) {
      var id = item.getAttribute("data-type-choice");
      item.setAttribute("aria-checked", String(id === "font:" + choice.font || id === "size:" + choice.size));
    });
  }
  function fillType(menu) {
    if (menu.pfTypeFilled) return;
    menu.pfTypeFilled = true;
    var kind = menu.getAttribute("data-type-menu") || "";
    var fonts = kind !== "size", sizes = kind !== "font";
    menu.setAttribute("role", "menu");
    menu.setAttribute("aria-label", kind === "size" ? "Size" : kind === "font" ? "Font" : "Font and size");
    function heading(text) {
      var node = document.createElement("div");
      node.setAttribute("data-type-label", "");
      node.textContent = text;
      menu.appendChild(node);
    }
    function choice(part, value, label) {
      var item = document.createElement("button");
      item.type = "button";
      item.setAttribute("role", "menuitemradio");
      item.setAttribute("data-type-choice", part + ":" + value);
      item.textContent = label;
      item.addEventListener("click", function () {
        setType(part, value);
        var open = menu.closest && menu.closest("details");
        if (open) open.open = false;
      });
      menu.appendChild(item);
    }
    if (fonts && sizes) heading("Font");
    if (fonts) Object.keys(FACES).forEach(function (name) { choice("font", name, FACES[name]); });
    if (fonts && sizes) heading("Size");
    if (sizes) Object.keys(SIZES).forEach(function (name) { choice("size", name, SIZES[name]); });
    paintType(menu, {font: root.getAttribute("data-font"), size: root.getAttribute("data-size")});
  }
  function bindType(el) {
    if (el.pfTypeBound) return;
    el.pfTypeBound = true;
    el.addEventListener("click", function () { cycleType(el.getAttribute("data-type-cycle")); });
  }
  function setType(kind, value) {
    var choice = {
      font: root.getAttribute("data-font") || pickType().font,
      size: root.getAttribute("data-size") || pickType().size
    };
    if (kind === "font") {
      value = knownFace(value);
      if (!value) return;
      choice.font = value;
      try { localStorage.setItem(FONT_KEY, value); } catch (e) {}
    } else {
      value = knownSize(value);
      if (!value) return;
      choice.size = value;
      try { localStorage.setItem(SIZE_KEY, value); } catch (e) {}
    }
    applyType(choice);
  }
  function cycleType(kind) {
    var size = kind === "size";
    var map = size ? SIZES : FACES;
    var now = root.getAttribute(size ? "data-size" : "data-font") || pickType()[size ? "size" : "font"];
    setType(size ? "size" : "font", nextName(map, now));
  }
  apply(pick());
  applyType(pickType());
  window.addEventListener("storage", function (event) {
    if (event.key === KEY && known(event.newValue)) apply(known(event.newValue));
    if (event.key === FONT_KEY || event.key === SIZE_KEY) applyType(pickType());
  });
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-theme-cycle]").forEach(bind);
    document.querySelectorAll("[data-theme-menu]").forEach(fill);
    document.querySelectorAll("[data-type-menu]").forEach(fillType);
    document.querySelectorAll("[data-type-cycle]").forEach(bindType);
    applyType(pickType());
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
  window.PortfolioTheme = { themes: THEMES, set: set, setGlobal: setGlobal, useGlobal: useGlobal, cycle: cycle, bind: bind, fill: fill, current: current, fillType: fillType, bindType: bindType, showType: function () { applyType(pickType()); }, setFont: function (name) { setType("font", name); }, setSize: function (name) { setType("size", name); }, cycleType: cycleType };
})();
