"use strict";
// Harness for Issue #739: the mobile transfer toast must stay dismissed after Close.
const fs = require("fs");
const vm = require("vm");

const src = fs.readFileSync(process.env.MEDIA_BROWSE_JS, "utf8");

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
    this.id = "";
    const self = this;
    this.classList = {
      add(...xs) {
        const set = new Set((self.className || "").split(/\s+/).filter(Boolean));
        xs.forEach((x) => set.add(x));
        self.className = [...set].join(" ");
      },
      remove(...xs) {
        const set = new Set((self.className || "").split(/\s+/).filter(Boolean));
        xs.forEach((x) => set.delete(x));
        self.className = [...set].join(" ");
      },
      contains(x) {
        return (self.className || "").split(/\s+/).includes(x);
      },
      toggle(name, force) {
        if (force === true) this.add(name);
        else if (force === false) this.remove(name);
        else if (this.contains(name)) this.remove(name);
        else this.add(name);
      },
    };
  }
  setAttribute(k, v) {
    this.attributes[k] = String(v);
  }
  getAttribute(k) {
    return Object.prototype.hasOwnProperty.call(this.attributes, k) ? this.attributes[k] : null;
  }
  addEventListener(type, fn) {
    (this._listeners[type] ||= []).push(fn);
  }
  click() {
    (this._listeners.click || []).forEach((fn) => fn({ type: "click", target: this }));
  }
  appendChild(el) {
    el.parentNode = this;
    el.isConnected = this.isConnected;
    this.children.push(el);
    return el;
  }
  remove() {
    this.isConnected = false;
    if (this.parentNode) {
      this.parentNode.children = this.parentNode.children.filter((c) => c !== this);
      this.parentNode = null;
    }
  }
  querySelector(sel) {
    return this.querySelectorAll(sel)[0] || null;
  }
  querySelectorAll(sel) {
    const out = [];
    const walk = (n) => {
      if (n.matches && n.matches(sel)) out.push(n);
      (n.children || []).forEach(walk);
    };
    (this.children || []).forEach(walk);
    return out;
  }
  matches(sel) {
    if (!sel) return false;
    if (sel.startsWith(".")) {
      return sel.slice(1).split(".").every((c) => (this.className || "").split(/\s+/).includes(c));
    }
    return this.tagName === String(sel).toUpperCase();
  }
  set innerHTML(html) {
    this._html = String(html);
    this.children = [];
    const re = /<([a-z0-9]+)([^>]*)>/gi;
    let match;
    while ((match = re.exec(html))) {
      const child = new El(match[1]);
      const cls = /class="([^"]*)"/.exec(match[2] || "");
      if (cls) child.className = cls[1];
      const aria = /aria-label="([^"]*)"/.exec(match[2] || "");
      if (aria) child.setAttribute("aria-label", aria[1]);
      child.parentNode = this;
      this.children.push(child);
    }
  }
  get innerHTML() {
    return this._html;
  }
  set textContent(v) {
    this._text = String(v);
  }
  get textContent() {
    return this._text;
  }
}

const body = new El("body");
const timers = [];
let now = 0;
function fakeSetTimeout(fn, ms) {
  const id = { fn, at: now + Number(ms || 0), live: true };
  timers.push(id);
  return id;
}
function fakeClearTimeout(id) {
  if (id) id.live = false;
}
async function flushAsync(ms) {
  now += Number(ms || 0);
  const due = timers.filter((t) => t.live && t.at <= now);
  due.forEach((t) => {
    t.live = false;
  });
  await Promise.all(due.map((t) => Promise.resolve(t.fn())));
}

const storage = {};
const sessionStorage = {
  getItem: (k) => (Object.prototype.hasOwnProperty.call(storage, k) ? storage[k] : null),
  setItem: (k, v) => {
    storage[k] = String(v);
  },
  removeItem: (k) => {
    delete storage[k];
  },
};

const document = {
  body,
  createElement: (tag) => new El(tag),
  querySelector: (sel) => body.querySelector(sel),
  querySelectorAll: (sel) => body.querySelectorAll(sel),
  addEventListener() {},
};

const win = {
  document,
  sessionStorage,
  addEventListener() {},
  dispatchEvent() {},
  CustomEvent: class {
    constructor(type, init) {
      this.type = type;
      this.detail = init && init.detail;
    }
  },
  DroneTransferToast: null,
};

function ensureToastContainer() {
  let container = body.querySelector(".toast-alert-container");
  if (!container) {
    container = new El("div");
    container.className = "toast-alert-container";
    body.appendChild(container);
  }
  return container;
}
function dismissToast(el) {
  if (el) el.remove();
}

async function api() {
  return {
    active: [
      {
        id: "job-1",
        status: "downloading",
        total_bytes: 100,
        downloaded_bytes: 10,
        transfer_speed_bps: 1,
        file_name: "Film.mp4",
      },
    ],
    queued: [],
    recent: [],
  };
}

const ctx = vm.createContext({
  window: win,
  document,
  console,
  setTimeout: fakeSetTimeout,
  clearTimeout: fakeClearTimeout,
  setInterval: () => 0,
  clearInterval() {},
  sessionStorage,
  CustomEvent: win.CustomEvent,
  escapeHtml: (s) => String(s),
  formatBytes: (n) => `${n}B`,
  ensureToastContainer,
  dismissToast,
  showToast() {},
  api,
  apiPost: async () => ({}),
  content: body,
  adminEnabled: true,
  location: { hash: "#movies" },
  MutationObserver: class {
    observe() {}
    disconnect() {}
  },
  IntersectionObserver: class {
    observe() {}
    disconnect() {}
  },
  requestAnimationFrame: () => 0,
  URLSearchParams,
  CSS: { escape: (s) => s },
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
});
win.window = win;

try {
  vm.runInContext(src, ctx, { filename: "media_browse.js" });
} catch (err) {
  if (!win.DroneTransferToast) {
    console.log(JSON.stringify({ error: String(err) }));
    process.exit(1);
  }
}

(async () => {
  const Toast = win.DroneTransferToast;
  Toast.track(["job-1"], "Copying");
  const closeBtn = body.querySelector(".tt-close");
  const report = {
    toastBeforeClose: body.querySelectorAll(".transfer-toast").length,
    closeButtonPresent: Boolean(closeBtn),
    closeHidden: Boolean(closeBtn && closeBtn.classList.contains("d-none")),
    closeAria: closeBtn ? closeBtn.getAttribute("aria-label") : null,
  };
  if (closeBtn) closeBtn.click();
  report.toastImmediatelyAfterClose = body.querySelectorAll(".transfer-toast").length;
  await flushAsync(400);
  report.toastAfterPollWindow = body.querySelectorAll(".transfer-toast").length;
  await flushAsync(1000);
  report.toastAfterNextPoll = body.querySelectorAll(".transfer-toast").length;
  console.log(JSON.stringify(report));
})().catch((err) => {
  console.log(JSON.stringify({ error: String(err && err.stack ? err.stack : err) }));
  process.exit(1);
});
