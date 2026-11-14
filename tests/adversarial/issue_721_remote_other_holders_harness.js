"use strict";
// Harness for Issue #721: Check other Drones on a remote item must append
// other holders (including This Drone) without duplicating the owner.
const fs = require("fs");
const vm = require("vm");

const src = fs.readFileSync(process.env.MEDIA_BROWSE_JS, "utf8").replace(
  "window.DroneRemoteMedia = { onRouteRendered, refresh()",
  "window.__mb = { fillAvailability, renderOwner, keyOf, onAction, openDetails, onRouteRendered };\n  window.DroneRemoteMedia = { onRouteRendered, refresh()"
);

class El {
  constructor(tag = "div") {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.dataset = {};
    this.attributes = {};
    this.className = "";
    this.style = {};
    this._text = "";
    this._html = "";
    this._listeners = {};
    this.isConnected = true;
    this.disabled = false;
    this.id = "";
    this.classList = {
      add: (...xs) => {
        const set = new Set(this.className.split(/\s+/).filter(Boolean));
        xs.forEach((x) => set.add(x));
        this.className = [...set].join(" ");
      },
      remove: (...xs) => {
        const set = new Set(this.className.split(/\s+/).filter(Boolean));
        xs.forEach((x) => set.delete(x));
        this.className = [...set].join(" ");
      },
      contains: (x) => this.className.split(/\s+/).includes(x),
      toggle() {},
    };
  }
  matches(sel) {
    if (!sel) return false;
    if (sel.startsWith(".")) {
      return sel.slice(1).split(".").every((c) => this.className.split(/\s+/).includes(c));
    }
    if (sel.startsWith("#")) return this.id === sel.slice(1);
    const dataEq = sel.match(/^\[data-act=["']([^"']+)["']\]$/);
    if (dataEq) return this.dataset.act === dataEq[1];
    const attr = sel.match(/^\[([^=\]]+)\]$/);
    if (attr) {
      const name = attr[1];
      if (name.startsWith("data-")) return this.dataset[name.slice(5)] != null;
      return this.attributes[name] != null;
    }
    if (sel.includes(".")) {
      const [tag, ...cls] = sel.split(".");
      if (tag && this.tagName !== tag.toUpperCase()) return false;
      return cls.every((c) => this.className.split(/\s+/).includes(c));
    }
    return this.tagName === sel.toUpperCase();
  }
  closest(sel) {
    let n = this;
    while (n) {
      if (n.matches && n.matches(sel)) return n;
      n = n.parentNode;
    }
    return null;
  }
  contains(el) {
    let n = el;
    while (n) {
      if (n === this) return true;
      n = n.parentNode;
    }
    return false;
  }
  querySelectorAll(sel) {
    const out = [];
    const walk = (n) => {
      for (const c of n.children || []) {
        if (c.matches && c.matches(sel)) out.push(c);
        walk(c);
      }
    };
    walk(this);
    return out;
  }
  querySelector(sel) {
    return this.querySelectorAll(sel)[0] || null;
  }
  appendChild(child) {
    child.parentNode = this;
    child.isConnected = this.isConnected;
    this.children.push(child);
    return child;
  }
  remove() {
    if (!this.parentNode) return;
    this.parentNode.children = this.parentNode.children.filter((c) => c !== this);
    this.parentNode = null;
    this.isConnected = false;
  }
  insertAdjacentElement(pos, el) {
    const p = this.parentNode;
    if (!p) return el;
    const i = p.children.indexOf(this);
    el.parentNode = p;
    el.isConnected = p.isConnected;
    if (pos === "afterend") p.children.splice(i + 1, 0, el);
    else this.appendChild(el);
    return el;
  }
  addEventListener(type, fn) {
    (this._listeners[type] ||= []).push(fn);
  }
  dispatchEvent(ev) {
    ev.target = ev.target || this;
    (this._listeners[ev.type] || []).forEach((fn) => fn(ev));
    return true;
  }
  setAttribute(k, v) {
    this.attributes[k] = String(v);
    if (k === "class") this.className = String(v);
    if (k.startsWith("data-")) {
      const name = k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      this.dataset[name] = String(v);
    }
  }
  get firstElementChild() {
    return this.children[0] || null;
  }
  get textContent() {
    if (this.children.length) return this.children.map((c) => c.textContent).join("");
    return this._text || this._html.replace(/<[^>]+>/g, "");
  }
  set textContent(v) {
    this._text = String(v);
    this._html = String(v);
    this.children = [];
  }
  get innerHTML() {
    if (this.children.length) return this.children.map(serialize).join("");
    return this._html;
  }
  set innerHTML(v) {
    this._html = String(v);
    this.children = parseHTML(String(v), this);
  }
}

function serialize(el) {
  const attrs = [];
  if (el.className) attrs.push(`class="${el.className}"`);
  Object.entries(el.dataset || {}).forEach(([k, v]) => attrs.push(`data-${k}="${v}"`));
  const inner = el.children.length ? el.children.map(serialize).join("") : (el._html || el._text || "");
  const tag = (el.tagName || "div").toLowerCase();
  return `<${tag}${attrs.length ? " " + attrs.join(" ") : ""}>${inner}</${tag}>`;
}

function parseAttrs(s, el) {
  const re = /([:@]?[\w-]+)(?:=["']([^"']*)["'])?/g;
  let m;
  while ((m = re.exec(s))) {
    const k = m[1];
    const v = m[2] == null ? "" : m[2];
    if (k === "class") el.className = v;
    else if (k === "id") el.id = v;
    else if (k.startsWith("data-")) {
      const name = k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      el.dataset[name] = v;
      el.attributes[k] = v;
    } else {
      el.attributes[k] = v;
    }
  }
}

function parseHTML(html, parent) {
  const kids = [];
  const re = /<([a-zA-Z0-9]+)([^>]*)\/>|<\/([a-zA-Z0-9]+)>|<([a-zA-Z0-9]+)([^>]*)>|([^<]+)/g;
  const stack = [{ el: { children: kids }, children: kids }];
  let m;
  while ((m = re.exec(html))) {
    if (m[1]) {
      const el = new El(m[1]);
      parseAttrs(m[2] || "", el);
      stack[stack.length - 1].children.push(el);
      continue;
    }
    if (m[3]) {
      if (stack.length > 1) stack.pop();
      continue;
    }
    if (m[4]) {
      const el = new El(m[4]);
      parseAttrs(m[5] || "", el);
      stack[stack.length - 1].children.push(el);
      const voidTags = new Set(["BR", "IMG", "INPUT", "META", "LINK", "HR"]);
      if (!voidTags.has(el.tagName) && !(m[5] || "").trim().endsWith("/")) {
        stack.push({ el, children: el.children });
      }
      continue;
    }
    if (m[6] && m[6].trim()) {
      const t = new El("#text");
      t._text = m[6];
      t._html = m[6];
      stack[stack.length - 1].children.push(t);
    }
  }
  kids.forEach((c) => {
    c.parentNode = parent;
    c.isConnected = parent.isConnected;
    const relink = (n) => n.children.forEach((ch) => {
      ch.parentNode = n;
      ch.isConnected = n.isConnected;
      relink(ch);
    });
    relink(c);
  });
  return kids;
}

function collect(root, cls) {
  const out = [];
  const walk = (n) => {
    for (const c of n.children || []) {
      if ((c.className || "").split(/\s+/).includes(cls)) out.push(c);
      walk(c);
    }
  };
  walk(root);
  return out;
}

const body = new El("body");
const content = new El("div");
content.id = "content";
content.appendChild(Object.assign(new El("div"), { className: "page", _text: "detail" }));
body.appendChild(content);

const loc = { hash: "" };
const apiCalls = [];
async function api(url) {
  apiCalls.push(String(url));
  if (String(url).includes("/also-on")) {
    const system = String(url).includes("/roms/") ? "genesis" : "";
    const copy = (drone_id, drone_name, is_local) => ({
      drone_id,
      drone_name,
      is_local,
      title: "Heat",
      display_title: "Heat",
      entry_key: "heat-1995",
      fingerprint: "fp-heat",
      ...(system ? { system } : {}),
    });
    return {
      copies: [
        copy("this-drone", "Living Room", true),
        copy("peer-1", "Arcade", false),
        copy("peer-2", "Bedroom", false),
      ],
      sources: [{ name: "Bedroom", status: "ok" }],
    };
  }
  return { sources: [] };
}

const win = {
  location: loc,
  addEventListener() {},
  missingArtworkRoms: [{ title: "Sonic", name: "Sonic", system: "genesis", fingerprint: "fp-sonic" }],
};
const document = {
  body,
  readyState: "complete",
  createElement: (t) => new El(t),
  getElementById: () => new El("div"),
  querySelector: (s) => body.querySelector(s) || content.querySelector(s),
  querySelectorAll: (s) => {
    if (s === ".drone-availability") return collect(content, "drone-availability").concat(collect(body, "drone-availability"));
    return body.querySelectorAll(s);
  },
  addEventListener() {},
  hidden: false,
};

const ctx = vm.createContext({
  window: win,
  document,
  content,
  console,
  location: loc,
  adminEnabled: true,
  escapeHtml: (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])),
  formatBytes: (n) => `${n} B`,
  showToast() {},
  ensureToastContainer: () => body,
  dismissToast() {},
  api,
  apiPost: async () => ({ jobs: [] }),
  parseMoviesHash: (hash) => (hash.startsWith("#movies/") ? { view: "detail", entryKey: hash.slice("#movies/".length) } : { view: "explore" }),
  parseMusicHash: (hash) => (hash.startsWith("#music/") ? { view: "detail", entryKey: hash.slice("#music/".length) } : { view: "explore" }),
  remoteKeyParts: (key) => {
    const m = String(key).match(/^r~([^~]+)~(.+)$/);
    return m ? { droneId: m[1], key: m[2] } : null;
  },
  fetchMediaDetail: async () => {
    throw new Error("fetchMediaDetail should not run when detailData is provided");
  },
  bootstrap: { Modal: { getOrCreateInstance() { return { show() {} }; } } },
  MutationObserver: class { observe() {} disconnect() {} },
  IntersectionObserver: class { observe() {} disconnect() {} },
  setTimeout: () => 0,
  clearTimeout() {},
  setInterval: () => 0,
  clearInterval() {},
  requestAnimationFrame: () => 0,
  URLSearchParams,
  Promise,
  JSON,
  Math,
  Date,
  Number,
  String,
  Array,
  Object,
  Set,
  Map,
  RegExp,
  Error,
  URL,
  sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  CSS: { escape: (s) => s },
});

try {
  vm.runInContext(src, ctx, { filename: "media_browse.js" });
} catch (err) {
  // observe() is unused by this harness
}

function countThisDrone(html) {
  return (String(html).match(/This Drone/g) || []).length;
}

function copyRowLabels(host) {
  return collect(host, "nm-copy").map((el) => el.textContent.replace(/\s+/g, " ").trim());
}

async function flush() {
  for (let i = 0; i < 12; i += 1) await new Promise((r) => setImmediate(r));
}

function snapshot(host) {
  const labels = copyRowLabels(host);
  return {
    alsoOn: apiCalls.filter((u) => u.includes("also-on")),
    thisDrone: countThisDrone(host.innerHTML),
    bedroom: host.innerHTML.includes("Bedroom"),
    arcadeRows: labels.filter((t) => t.includes("Arcade")).length,
    thisDroneRows: labels.filter((t) => t.includes("This Drone")).length,
    copy: host.innerHTML.includes("Copy to this Drone"),
    spinner: host.innerHTML.includes("Checking which Drones"),
    checkBtn: !!host.querySelector('[data-act="check-others"]'),
    labels,
    hostHtml: host.innerHTML,
  };
}

async function checkRemotePage(hash, detail) {
  loc.hash = hash;
  apiCalls.length = 0;
  await win.DroneRemoteMedia.onRouteRendered(hash, detail);
  const card = collect(content, "drone-availability").concat(collect(body, "drone-availability")).pop();
  if (!card) return { error: "no availability card" };
  const host = card.querySelector(".nm-avail");
  const opened = snapshot(host);
  const btn = host.querySelector('[data-act="check-others"]');
  apiCalls.length = 0;
  card.dispatchEvent({ type: "click", target: btn, preventDefault() {}, stopPropagation() {} });
  await flush();
  return { opened, checked: snapshot(host) };
}

async function checkGamesDialog() {
  apiCalls.length = 0;
  win.__mb.openDetails("roms", {
    title: "Sonic",
    items: [{
      title: "Sonic",
      system: "genesis",
      fingerprint: "fp-sonic",
      drone_id: "peer-1",
      drone_name: "Arcade",
      is_local: false,
      entry_key: "heat-1995",
    }],
  });
  const modal = collect(body, "modal").pop() || body.querySelector(".modal");
  const host = modal && modal.querySelector(".nm-avail");
  if (!host) return { error: "no games availability host" };
  const opened = snapshot(host);
  const btn = host.querySelector('[data-act="check-others"]');
  apiCalls.length = 0;
  modal.dispatchEvent({ type: "click", target: btn, preventDefault() {}, stopPropagation() {} });
  await flush();
  return { opened, checked: snapshot(host) };
}

(async () => {
  const movie = await checkRemotePage("#movies/r~peer-1~heat-1995", {
    display_title: "Heat",
    movie_name: "Heat",
    fingerprint: "fp-heat",
    artist: "",
    entry_key: "heat-1995",
    drone_name: "Arcade",
  });
  const music = await checkRemotePage("#music/r~peer-1~heat-1995", {
    display_title: "Heat",
    track_name: "Heat",
    fingerprint: "fp-heat",
    artist: "A",
    entry_key: "heat-1995",
    drone_name: "Arcade",
  });
  const games = await checkGamesDialog();
  console.log(JSON.stringify({ movie, music, games }));
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
