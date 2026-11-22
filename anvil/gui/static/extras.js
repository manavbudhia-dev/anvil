// Anvil: search, artifacts, connectors, plugins, scheduled tasks and browser panels.
// Loaded after the main script in index.html and uses its helpers (api, el, esc, md, toast, state, openSession).
(function () {
"use strict";

const css = `
#x-nav { display: flex; flex-direction: column; gap: 1px; padding: 0 8px 6px; }
#x-nav button { display: flex; align-items: center; gap: 10px; padding: 6px 10px; border: 0; border-radius: 8px; background: none; color: var(--muted); font-size: 13.5px; text-align: left; }
#x-nav button:hover { background: var(--side-hover); color: var(--text); }
#x-nav button .k { margin-left: auto; font: 11px var(--mono); color: var(--faint); }
#x-nav button .n { margin-left: auto; font-size: 11px; color: var(--faint); background: var(--side-hover); border-radius: 999px; padding: 0 7px; }
.x-modal { max-width: 860px !important; display: flex; flex-direction: column; }
.x-head { display: flex; align-items: center; gap: 10px; padding: 16px 20px 10px; border-bottom: 1px solid var(--border); }
.x-head h2 { padding: 0 !important; flex: 1; }
.x-body { padding: 14px 20px 18px; overflow-y: auto; }
.x-sub { color: var(--muted); font-size: 13px; margin: 0 0 12px; }
.x-row { display: flex; align-items: center; gap: 10px; padding: 10px 12px; border: 1px solid var(--border); border-radius: 11px; margin-bottom: 8px; background: var(--bg); }
.x-row .main { flex: 1; min-width: 0; }
.x-row .t { font-weight: 600; font-size: 14px; display: flex; align-items: center; gap: 8px; }
.x-row .d { font-size: 12.5px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; }
.x-row .d.err { color: var(--err); white-space: normal; }
.x-row .acts { display: flex; gap: 6px; flex-shrink: 0; flex-wrap: wrap; justify-content: flex-end; }
.x-row .acts .btn { padding: 4px 10px; font-size: 12.5px; }
.x-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--faint); flex-shrink: 0; }
.x-dot.ready { background: var(--ok); } .x-dot.error, .x-dot.needs_auth { background: var(--err); }
.x-dot.connecting, .x-dot.signing_in { background: var(--accent); animation: pulse 1.2s infinite; }
.x-badge { font-size: 11px; font-weight: 500; color: var(--muted); border: 1px solid var(--border); border-radius: 999px; padding: 0 7px; }
.x-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 8px; }
.x-tile { text-align: left; border: 1px solid var(--border); border-radius: 11px; padding: 11px 12px; background: var(--panel); display: flex; flex-direction: column; gap: 4px; }
.x-tile:hover { border-color: var(--accent); }
.x-tile .t { font-weight: 600; display: flex; align-items: center; gap: 8px; }
.x-tile .d { font-size: 12px; color: var(--muted); line-height: 1.4; }
.x-ico { width: 26px; height: 26px; border-radius: 7px; background: var(--accent-soft); color: var(--accent); display: grid; place-items: center; flex-shrink: 0; }
.x-section { font-size: 11.5px; font-weight: 600; color: var(--faint); text-transform: uppercase; letter-spacing: .04em; margin: 16px 0 8px; }
.x-form { border: 1px solid var(--border); border-radius: 12px; padding: 14px; margin: 10px 0; background: var(--bg); }
.x-form textarea { width: 100%; min-height: 84px; padding: 8px 11px; border: 1px solid var(--border-strong); border-radius: 9px; background: var(--panel); outline: none; resize: vertical; font: 13.5px/1.5 var(--sans); }
.x-form .field input[type=text], .x-form .field input[type=password], .x-form .field select { background: var(--panel); }
.x-note { font-size: 12.5px; color: var(--warn); background: var(--warn-bg); border-radius: 9px; padding: 8px 11px; margin: 0 0 12px; }
.x-empty { color: var(--faint); font-size: 13px; padding: 18px 4px; text-align: center; }
.x-search { width: 100%; padding: 11px 14px; font-size: 16px; border: 1px solid var(--border-strong); border-radius: 11px; background: var(--bg); outline: none; }
.x-search:focus { border-color: var(--accent); }
.x-res { padding: 9px 11px; border-radius: 9px; cursor: pointer; display: flex; gap: 10px; align-items: flex-start; }
.x-res:hover, .x-res.sel { background: var(--side-hover); }
.x-res .main { min-width: 0; flex: 1; }
.x-res .t { font-weight: 600; font-size: 13.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.x-res .s { font-size: 12.5px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
.x-res .s mark { background: var(--accent-soft); color: var(--accent); border-radius: 3px; }
.x-split { display: grid; grid-template-columns: 250px 1fr; gap: 14px; min-height: 60vh; }
.x-list { overflow-y: auto; max-height: 68vh; }
.x-list .x-res { flex-direction: column; gap: 1px; }
.x-view { border: 1px solid var(--border); border-radius: 12px; overflow: hidden; display: flex; flex-direction: column; min-width: 0; background: var(--bg); }
.x-view .bar { display: flex; align-items: center; gap: 6px; padding: 8px 10px; border-bottom: 1px solid var(--border); }
.x-view .bar .t { flex: 1; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.x-view .bar .btn { padding: 3px 10px; font-size: 12.5px; }
.x-view iframe { border: 0; width: 100%; flex: 1; min-height: 60vh; background: #fff; }
.x-view .doc { padding: 14px 18px; overflow: auto; max-height: 68vh; }
.x-view pre { margin: 0; padding: 14px; overflow: auto; max-height: 68vh; font: 12.5px/1.5 var(--mono); }
.x-switch { position: relative; display: inline-block; width: 34px; height: 20px; flex-shrink: 0; align-self: center; }
.x-switch input { opacity: 0; width: 0; height: 0; }
.x-switch span { position: absolute; inset: 0; background: var(--border-strong); border-radius: 999px; transition: .15s; cursor: pointer; }
.x-switch span::before { content: ""; position: absolute; width: 14px; height: 14px; left: 3px; top: 3px; background: #fff; border-radius: 50%; transition: .15s; }
.x-switch input:checked + span { background: var(--accent); }
.x-switch input:checked + span::before { transform: translateX(14px); }
.x-log { font: 12px/1.5 var(--mono); background: var(--code-bg); border-radius: 9px; padding: 10px; white-space: pre-wrap; max-height: 200px; overflow: auto; }
.x-toast { position: fixed; right: 20px; bottom: 20px; z-index: 61; background: var(--panel); border: 1px solid var(--border-strong); box-shadow: var(--shadow); border-radius: 12px; padding: 10px 12px; display: flex; gap: 10px; align-items: center; font-size: 13.5px; max-width: 360px; }
.x-toast .btn { padding: 3px 10px; font-size: 12.5px; }
@media (max-width: 760px) {
  .x-split { grid-template-columns: 1fr; }
  .x-split.viewing .x-list { display: none; }
  .x-split:not(.viewing) .x-view { display: none; }
  .x-row { flex-wrap: wrap; }
  .x-row .acts { width: 100%; justify-content: flex-start; }
}`;
document.head.appendChild(Object.assign(document.createElement("style"), { textContent: css }));

const svg = (d, w = 15) => `<svg width="${w}" height="${w}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${d}</svg>`;
const ICONS = {
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  artifact: '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6M9 14l-2 2 2 2m6-4 2 2-2 2"/>',
  plug: '<path d="M9 2v6m6-6v6M6 8h12v3a6 6 0 0 1-12 0zm6 9v5"/>',
  puzzle: '<path d="M10 3h4v3a2 2 0 1 0 4 0V3h3v7h-3a2 2 0 1 0 0 4h3v7h-7v-3a2 2 0 1 0-4 0v3H3v-7h3a2 2 0 1 0 0-4H3V3z"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
  mail: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 7 9 6 9-6"/>',
  doc: '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6M8 13h8M8 17h5"/>',
  chat: '<path d="M21 12a8 8 0 0 1-11.6 7.1L4 21l1.9-5.4A8 8 0 1 1 21 12z"/>',
  code: '<path d="m8 8-5 4 5 4m8-8 5 4-5 4M14 4l-4 16"/>',
  check: '<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  session: '<path d="M21 12a8 8 0 0 1-11.6 7.1L4 21l1.9-5.4A8 8 0 1 1 21 12z"/>',
  project: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  schedule: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
};
const ico = (name, w) => svg(ICONS[name] || ICONS.plug, w);
const fmtWhen = (iso) => { if (!iso) return "—"; const d = new Date(iso); if (isNaN(d)) return "—"; return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }); };
const openLink = (url) => { if (IS_REMOTE) window.open(url, "_blank", "noopener"); else api("/api/open", { method: "POST", body: { url } }).catch(() => window.open(url, "_blank", "noopener")); };
const sw = (checked, onChange, title = "") => { const l = el("label", "x-switch"); l.title = title; l.innerHTML = `<input type="checkbox" ${checked ? "checked" : ""}><span></span>`; l.querySelector("input").onchange = (e) => onChange(e.target.checked); return l; };
const btn = (label, cls, fn) => { const b = el("button", "btn" + (cls ? " " + cls : "")); b.textContent = label; b.onclick = fn; return b; };
async function act(promise, ok) { try { const r = await promise; if (ok) toast(ok); return r; } catch (e) { toast(e.message); throw e; } }

// ---------- sidebar & panel shell ----------
const nav = el("nav"); nav.id = "x-nav";
const NAV = [["search", "Search", "search"], ["artifacts", "Artifacts", "artifact"], ["connectors", "Connectors", "plug"],
  ["plugins", "Plugins", "puzzle"], ["scheduled", "Scheduled", "clock"], ["browser", "Browser", "globe"]];
for (const [id, label, icon] of NAV) {
  const b = el("button"); b.dataset.panel = id;
  b.innerHTML = `${ico(icon)}<span>${label}</span>${id === "search" ? `<span class="k">${/Mac/.test(navigator.platform) ? "⌘" : "Ctrl "}K</span>` : `<span class="n" hidden></span>`}`;
  b.onclick = () => { if (typeof closeSideMobile === "function") closeSideMobile(); open(id); };
  nav.appendChild(b);
}
document.querySelector("#new-btn").after(nav);

const overlay = el("div", "overlay"); overlay.id = "x-panel";
overlay.innerHTML = `<div class="modal x-modal"><div class="x-head"><span class="x-ico"></span><h2></h2><button class="icon-btn" title="Close">${svg('<path d="M18 6 6 18M6 6l12 12"/>', 16)}</button></div><div class="x-body"></div></div>`;
document.body.appendChild(overlay);
overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) close(); });
overlay.querySelector(".x-head .icon-btn").onclick = close;
const body = overlay.querySelector(".x-body");
let current = null;
function close() { overlay.classList.remove("open"); current = null; }
function shell(id, title, icon) {
  current = id; overlay.querySelector("h2").textContent = title; overlay.querySelector(".x-head .x-ico").innerHTML = ico(icon, 15);
  body.innerHTML = ""; overlay.classList.add("open");
}
const PANELS = {};
function open(id, arg) { PANELS[id](arg); }
function refresh() { if (current && PANELS[current] && current !== "search") PANELS[current]._refresh?.(); }

document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); open("search"); }
});

// ---------- search ----------
PANELS.search = function () {
  shell("search", "Search", "search");
  body.innerHTML = `<input class="x-search" placeholder="Search sessions, artifacts, scheduled tasks and projects" spellcheck="false"><div class="x-results" style="margin-top:10px"></div>`;
  const input = body.querySelector("input"), box = body.querySelector(".x-results");
  let timer, sel = 0, results = [];
  const hl = (s, q) => { let h = esc(s || ""); for (const t of q.split(/\s+/).filter(Boolean)) h = h.replace(new RegExp(t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi"), (m) => `<mark>${m}</mark>`); return h; };
  const label = { session: "Session", artifact: "Artifact", schedule: "Scheduled", project: "Project" };
  function render(q) {
    box.innerHTML = "";
    if (!q) { box.innerHTML = `<div class="x-empty">Type to search everything Anvil has saved.</div>`; return; }
    if (!results.length) { box.innerHTML = `<div class="x-empty">No matches for “${esc(q)}”.</div>`; return; }
    results.forEach((r, i) => {
      const d = el("div", "x-res" + (i === sel ? " sel" : ""));
      d.innerHTML = `<span class="x-ico">${ico(r.type, 14)}</span><div class="main"><div class="t"></div><div class="s">${hl(r.snippet, q)}</div></div><span class="x-badge">${label[r.type] || r.type}</span>`;
      d.querySelector(".t").textContent = r.title || "(untitled)";
      d.onclick = () => go(r); box.appendChild(d);
    });
  }
  function go(r) {
    if (r.type === "session") { close(); openSession(r.id); }
    else if (r.type === "artifact") open("artifacts", r.id);
    else if (r.type === "schedule") open("scheduled");
    else if (r.type === "project" && typeof window.openProject === "function") { close(); window.openProject(r.id); }
    else toast(r.root ? `Project folder: ${r.root}` : "Opened");
  }
  input.oninput = () => { clearTimeout(timer); timer = setTimeout(async () => {
    const q = input.value.trim(); sel = 0;
    results = q ? (await api(`/api/search?q=${encodeURIComponent(q)}`)).results : []; render(q);
  }, 160); };
  input.onkeydown = (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); sel = Math.min(sel + 1, results.length - 1); render(input.value.trim()); }
    if (e.key === "ArrowUp") { e.preventDefault(); sel = Math.max(sel - 1, 0); render(input.value.trim()); }
    if (e.key === "Enter" && results[sel]) go(results[sel]);
  };
  render(""); setTimeout(() => input.focus(), 30);
};

// ---------- artifacts ----------
PANELS.artifacts = async function (openId) {
  shell("artifacts", "Artifacts", "artifact");
  body.innerHTML = `<p class="x-sub">Pages, reports and diagrams Anvil saved for you. Ask it to “save this as an artifact” in any session.</p><div class="x-split"><div class="x-list"></div><div class="x-view"><div class="x-empty">Pick an artifact to preview it.</div></div></div>`;
  const list = body.querySelector(".x-list"), view = body.querySelector(".x-view"), split = body.querySelector(".x-split");
  let items = [], selected = openId || null;
  async function load() {
    items = (await api("/api/artifacts")).artifacts; list.innerHTML = "";
    if (!items.length) { list.innerHTML = `<div class="x-empty">No artifacts yet.</div>`; return; }
    for (const a of items) {
      const d = el("div", "x-res" + (a.id === selected ? " sel" : ""));
      d.innerHTML = `<div class="t"></div><div class="s"></div>`;
      d.querySelector(".t").textContent = a.title; d.querySelector(".s").textContent = `${a.kind} · v${a.version} · ${fmtWhen(a.updated_at)}`;
      d.onclick = () => { selected = a.id; show(a.id); list.querySelectorAll(".x-res").forEach((x) => x.classList.toggle("sel", x === d)); };
      list.appendChild(d);
    }
  }
  async function show(id) {
    const a = (await api(`/api/artifacts/${id}`)).artifact; split.classList.add("viewing");
    view.innerHTML = `<div class="bar"><button class="btn x-back" style="display:none">←</button><span class="t"></span></div>`;
    const bar = view.querySelector(".bar"); bar.querySelector(".t").textContent = a.title;
    if (matchMedia("(max-width: 760px)").matches) { const b = bar.querySelector(".x-back"); b.style.display = ""; b.onclick = () => split.classList.remove("viewing"); }
    const ext = { html: "html", markdown: "md", svg: "svg", code: a.language || "txt", text: "txt" }[a.kind] || "txt";
    bar.append(
      btn("Copy", "", () => navigator.clipboard.writeText(a.content).then(() => toast("Copied"))),
      btn("Download", "", () => { const blob = new Blob([a.content], { type: "text/plain" }); const u = URL.createObjectURL(blob); const l = Object.assign(document.createElement("a"), { href: u, download: `${a.title.replace(/[^\w.-]+/g, "-")}.${ext}` }); l.click(); setTimeout(() => URL.revokeObjectURL(u), 1000); }),
      a.session_id ? btn("Session", "", () => { close(); openSession(a.session_id).catch(() => toast("That session was deleted")); }) : "",
      btn("Delete", "", async () => { if (!confirm(`Delete “${a.title}”?`)) return; await api(`/api/artifacts/${a.id}`, { method: "DELETE" }); selected = null; view.innerHTML = `<div class="x-empty">Deleted.</div>`; split.classList.remove("viewing"); load(); }),
    );
    if (a.kind === "html" || a.kind === "svg") {
      const f = document.createElement("iframe");
      f.setAttribute("sandbox", "allow-scripts allow-forms allow-popups allow-modals");  // no allow-same-origin: no access to Anvil
      f.srcdoc = a.kind === "svg" ? `<!doctype html><body style="margin:0;display:grid;place-items:center;min-height:100vh">${a.content}</body>` : a.content;
      view.appendChild(f);
    } else if (a.kind === "markdown") {
      view.appendChild(el("div", "doc msg md", md(a.content)));
    } else {
      const pre = el("pre"); pre.textContent = a.content; view.appendChild(pre);
    }
  }
  PANELS.artifacts._refresh = load;
  await load(); if (selected) show(selected);
};

// ---------- connectors ----------
PANELS.connectors = async function () {
  shell("connectors", "Connectors", "plug");
  let data = null, adding = null;
  async function load() { data = await api("/api/connectors"); render(); }
  function stateText(c) {
    if (!c.enabled) return "Off";
    return { ready: `Connected · ${c.tools.length} tools`, connecting: "Connecting…", signing_in: "Waiting for you to finish signing in…",
      needs_auth: "Sign in needed", error: "Couldn't connect", idle: c.auth === "oauth" && !c.signed_in ? "Not signed in" : "Connects when a session needs it" }[c.state] || c.state;
  }
  async function signIn(c) {
    const r = await act(api(`/api/connectors/${c.id}/login`, { method: "POST" }));
    openLink(r.url); toast("Finish signing in in your browser"); load();
  }
  function render() {
    body.innerHTML = `<p class="x-sub">Connectors give Anvil tools from other apps through MCP servers. Anvil asks before a connector changes anything, unless the server marks the tool read-only.</p>`;
    const mine = data.connectors;
    if (mine.length) {
      body.appendChild(el("div", "x-section", "Your connectors"));
      for (const c of mine) {
        const r = el("div", "x-row");
        r.innerHTML = `<span class="x-ico">${ico(c.icon, 14)}</span><div class="main"><div class="t"><span class="x-dot ${c.enabled ? c.state : ""}"></span><span class="nm"></span>${c.source !== "user" ? `<span class="x-badge">${esc(c.source)}</span>` : ""}</div><div class="d st"></div>${c.error && c.enabled ? `<div class="d err"></div>` : ""}</div><div class="acts"></div>`;
        r.querySelector(".nm").textContent = c.name; r.querySelector(".st").textContent = stateText(c) + (c.transport === "http" ? ` · ${c.url}` : ` · ${c.command}`);
        if (c.error && c.enabled) r.querySelector(".err").textContent = c.error;
        const acts = r.querySelector(".acts");
        if (c.transport === "http" && c.auth === "oauth") acts.appendChild(c.signed_in ? btn("Sign out", "", () => act(api(`/api/connectors/${c.id}/logout`, { method: "POST" }), "Signed out").then(load)) : btn("Sign in", "primary", () => signIn(c)));
        else if (c.state === "needs_auth" && c.transport === "http") acts.appendChild(btn("Sign in", "primary", () => signIn(c)));
        acts.appendChild(btn("Test", "", async () => { r.querySelector(".x-dot").className = "x-dot connecting"; await act(api(`/api/connectors/${c.id}/test`, { method: "POST" })); load(); }));
        if (c.source === "user") {
          acts.appendChild(btn("Edit", "", () => { adding = { preset: data.presets.find((p) => p.id === c.preset) || data.presets.find((p) => p.id === (c.transport === "http" ? "custom-http" : "custom-stdio")), existing: c }; render(); }));
          acts.appendChild(btn("Remove", "", async () => { if (!confirm(`Remove ${c.name}? Its saved sign-in and keys are deleted too.`)) return; await act(api(`/api/connectors/${c.id}`, { method: "DELETE" }), "Removed"); load(); }));
        }
        acts.appendChild(sw(c.enabled, (on) => act(api(`/api/connectors/${c.id}/enabled`, { method: "POST", body: { enabled: on } })).then(load), "On/off"));
        body.appendChild(r);
      }
    }
    if (adding) { body.appendChild(form(adding.preset, adding.existing)); return; }
    body.appendChild(el("div", "x-section", "Add a connector"));
    const grid = el("div", "x-grid");
    for (const p of data.presets) {
      const t = el("button", "x-tile");
      t.innerHTML = `<div class="t"><span class="x-ico">${ico(p.icon, 14)}</span><span></span></div><div class="d"></div>`;
      t.querySelector(".t span:last-child").textContent = p.name; t.querySelector(".d").textContent = p.description;
      t.onclick = () => { adding = { preset: p }; render(); };
      grid.appendChild(t);
    }
    body.appendChild(grid);
  }
  function form(p, existing) {
    const f = el("div", "x-form");
    f.innerHTML = `<div style="display:flex;align-items:center;gap:8px;margin-bottom:10px"><span class="x-ico">${ico(p.icon, 14)}</span><strong style="flex:1"></strong>${p.docs ? `<a href="#" class="x-docs" style="font-size:12.5px;color:var(--accent)">Setup guide</a>` : ""}</div>${p.notes ? `<div class="x-note"></div>` : ""}<div class="flds"></div><div style="display:flex;gap:8px;justify-content:flex-end"></div>`;
    f.querySelector("strong").textContent = existing ? `Edit ${existing.name}` : p.name;
    if (p.notes) f.querySelector(".x-note").textContent = p.notes;
    if (p.docs) f.querySelector(".x-docs").onclick = (e) => { e.preventDefault(); openLink(p.docs); };
    const flds = f.querySelector(".flds"); const inputs = {};
    const add = (key, label, opts = {}) => {
      const d = el("div", "field"); d.innerHTML = `<label></label>${opts.area ? "<textarea></textarea>" : `<input type="${opts.secret ? "password" : "text"}" spellcheck="false" autocomplete="off">`}${opts.help ? `<div class="help"></div>` : ""}`;
      d.querySelector("label").textContent = label + (opts.required ? "" : opts.optional ? " (optional)" : "");
      const inp = d.querySelector("input, textarea"); inp.value = opts.value || ""; if (opts.placeholder) inp.placeholder = opts.placeholder;
      if (opts.help) d.querySelector(".help").textContent = opts.help;
      inputs[key] = inp; flds.appendChild(d);
    };
    add("name", "Name", { value: existing?.name || (p.custom ? "" : p.name), placeholder: "e.g. Sentry", required: true });
    if (p.custom && p.transport === "http") {
      add("url", "Server URL", { value: existing?.url || "", placeholder: "https://example.com/mcp", required: true });
      add("token", "Bearer token or API key", { secret: true, optional: true, help: existing?.token_set ? "Saved. Leave empty to keep it." : "Leave empty to sign in with your browser (MCP OAuth)." });
      add("client_id", "OAuth client ID", { optional: true, value: existing?.client_id || "", help: "Only for servers that don't support automatic app registration." });
      add("client_secret", "OAuth client secret", { secret: true, optional: true });
    } else if (p.custom) {
      add("command", "Command", { value: existing?.command || "", placeholder: "npx -y @scope/some-mcp-server --flag", required: true });
      add("env", "Environment variables", { area: true, optional: true, placeholder: "ONE_PER_LINE=value\nAPI_KEY=…", help: "Variables with KEY, TOKEN, SECRET or PASSWORD in the name are stored privately." });
    }
    for (const fd of p.fields) {
      const saved = existing && fd.secret && ((fd.target === "bearer" && existing.token_set) || (fd.target === "env" && existing.secret_env.includes(fd.key)));
      const value = existing ? (fd.target === "env" && !fd.secret ? existing.env[fd.key] || "" : fd.target === "oauth_client_id" ? existing.client_id : "") : (fd.default || "");
      add("v:" + fd.key, fd.label, { secret: fd.secret, required: fd.required, value, help: saved ? "Saved. Leave empty to keep it." : fd.help });
    }
    if (p.auth === "oauth" && !p.custom) {
      flds.appendChild(el("div", "help", `After saving, click Sign in. Anvil opens ${esc(new URL(p.url).host)} in your browser and receives the result on ${esc(data.redirect_uri)} (sign in from the computer running Anvil).`));
      flds.lastChild.style.margin = "0 0 12px";
    }
    const foot = f.lastElementChild;
    foot.append(btn("Cancel", "", () => { adding = null; render(); }), btn(existing ? "Save" : "Add connector", "primary", async () => {
      const values = {}; for (const [k, inp] of Object.entries(inputs)) if (k.startsWith("v:")) values[k.slice(2)] = inp.value; else if (["token", "client_id", "client_secret"].includes(k)) values[k] = inp.value;
      const payload = { id: existing?.id, preset: p.id, name: inputs.name.value, url: inputs.url?.value, command: inputs.command?.value, env: inputs.env ? inputs.env.value : undefined, values };
      const r = await act(api("/api/connectors", { method: "POST", body: payload }), existing ? "Saved" : "Connector added");
      adding = null; await load();
      if (!existing && r.connector.transport === "http" && r.connector.auth === "oauth" && !r.connector.signed_in) signIn(r.connector);
    }));
    setTimeout(() => (inputs[p.custom ? "name" : Object.keys(inputs)[1]] || inputs.name).focus(), 30);
    return f;
  }
  PANELS.connectors._refresh = () => { if (!adding) load(); };
  await load();
};

// ---------- plugins ----------
PANELS.plugins = async function () {
  shell("plugins", "Plugins", "puzzle");
  async function load() {
    const { plugins } = await api("/api/plugins");
    body.innerHTML = `<p class="x-sub">Plugins bundle skills, slash commands and connectors. Install from a git URL or a folder; Claude Code plugins work too.</p>
      <div class="x-form"><div class="row" style="gap:8px"><input type="text" class="src" placeholder="https://github.com/someone/anvil-plugin.git or a folder path" spellcheck="false" style="flex:3;padding:8px 11px;border:1px solid var(--border-strong);border-radius:9px;background:var(--panel)"><input type="text" class="pl" placeholder="Plugin name (optional)" spellcheck="false" style="flex:1;padding:8px 11px;border:1px solid var(--border-strong);border-radius:9px;background:var(--panel)"><button class="btn primary" style="flex:0">Install</button></div><div class="help">The name is only needed for repositories that hold several plugins.</div></div>
      <div class="x-section">Installed</div><div class="lst"></div>`;
    const go = body.querySelector(".x-form .btn");
    go.onclick = async () => { const src = body.querySelector(".src").value.trim(); if (!src) return; go.disabled = true; go.textContent = "Installing…";
      try { const r = await act(api("/api/plugins/install", { method: "POST", body: { source: src, plugin: body.querySelector(".pl").value } })); toast(`Installed ${r.plugin.name}`); if (typeof loadSkills === "function") loadSkills(); load(); }
      catch { go.disabled = false; go.textContent = "Install"; } };
    const lst = body.querySelector(".lst");
    if (!plugins.length) { lst.innerHTML = `<div class="x-empty">No plugins installed yet.</div>`; return; }
    for (const p of plugins) {
      const r = el("div", "x-row");
      const bits = [p.skills.length && `${p.skills.length} skill${p.skills.length > 1 ? "s" : ""}`, p.commands.length && `commands: ${p.commands.map((c) => "/" + c).join(" ")}`, p.connectors.length && `connectors: ${p.connectors.join(", ")}`].filter(Boolean).join(" · ");
      r.innerHTML = `<span class="x-ico">${ico("puzzle", 14)}</span><div class="main"><div class="t"><span class="nm"></span>${p.version ? `<span class="x-badge">v${esc(p.version)}</span>` : ""}</div><div class="d ds"></div><div class="d"></div></div><div class="acts"></div>`;
      r.querySelector(".nm").textContent = p.name; r.querySelector(".ds").textContent = p.description || p.source;
      r.querySelectorAll(".d")[1].textContent = bits || "Empty plugin";
      const acts = r.querySelector(".acts");
      if (p.source) acts.appendChild(btn("Update", "", async () => { await act(api(`/api/plugins/${p.name}/update`, { method: "POST" }), "Updated"); load(); }));
      acts.appendChild(btn("Remove", "", async () => { if (!confirm(`Remove plugin ${p.name}?`)) return; await act(api(`/api/plugins/${p.name}`, { method: "DELETE" }), "Removed"); load(); }));
      acts.appendChild(sw(p.enabled, (on) => act(api(`/api/plugins/${p.name}/enabled`, { method: "POST", body: { enabled: on } })).then(load)));
      lst.appendChild(r);
    }
  }
  await load();
};

// ---------- scheduled tasks ----------
PANELS.scheduled = async function () {
  shell("scheduled", "Scheduled tasks", "clock");
  let editing = null, data = null;
  async function load() { data = await api("/api/schedules"); render(); }
  function render() {
    body.innerHTML = `<p class="x-sub">Run a prompt on a schedule in a project folder. Tasks run while Anvil is open here, or on a server with <code>anvil schedule daemon</code>. Each run becomes a session you can open.</p>`;
    if (editing) { body.appendChild(form(editing === true ? null : editing)); }
    else body.appendChild(btn("New scheduled task", "primary", () => { editing = true; render(); }));
    body.appendChild(el("div", "x-section", "Tasks"));
    if (!data.schedules.length) { body.appendChild(el("div", "x-empty", "No scheduled tasks yet.")); return; }
    for (const s of data.schedules) {
      const r = el("div", "x-row");
      r.innerHTML = `<span class="x-ico">${ico("clock", 14)}</span><div class="main"><div class="t"><span class="nm"></span><span class="x-badge">${esc(s.mode)}</span></div><div class="d a"></div><div class="d b"></div></div><div class="acts"></div>`;
      r.querySelector(".nm").textContent = s.name;
      r.querySelector(".a").textContent = `${s.description} · ${s.enabled ? "next " + fmtWhen(s.next_run) : "paused"} · ${base(s.root)}`;
      r.querySelector(".b").textContent = `Last run: ${s.last_run ? fmtWhen(s.last_run) + " · " + (s.last_status || "") : "never"}`;
      const acts = r.querySelector(".acts");
      if (s.last_session) acts.appendChild(btn("Last run", "", () => { close(); openSession(s.last_session).catch(() => toast("That session was deleted")); }));
      acts.appendChild(btn("Run now", "", async () => { const x = await act(api(`/api/schedules/${s.id}/run`, { method: "POST" }), "Started"); await loadState(); close(); openSession(x.session); }));
      acts.appendChild(btn("Edit", "", () => { editing = s; render(); }));
      acts.appendChild(btn("Delete", "", async () => { if (!confirm(`Delete “${s.name}”?`)) return; await act(api(`/api/schedules/${s.id}`, { method: "DELETE" }), "Deleted"); load(); }));
      acts.appendChild(sw(s.enabled, (on) => act(api("/api/schedules", { method: "POST", body: { ...s, enabled: on } })).then(load)));
      body.appendChild(r);
    }
  }
  function form(s) {
    const f = el("div", "x-form");
    const presetOf = (c) => Object.entries(data.presets).find(([, v]) => v === c)?.[0] || "custom";
    f.innerHTML = `
      <div class="field"><label>Name</label><input type="text" class="nm" placeholder="Morning dependency check"></div>
      <div class="field"><label>Prompt</label><textarea class="pr" placeholder="Run the test suite and summarize any failures. Update outdated dependencies on a branch."></textarea></div>
      <div class="field"><label>Project folder</label><div class="row" style="gap:8px"><input type="text" class="rt" spellcheck="false" style="flex:1">${state.canPick ? '<button class="btn pick" style="flex:0">Browse…</button>' : ""}</div></div>
      <div class="row"><div class="field"><label>When</label><select class="when"><option value="hourly">Every hour</option><option value="daily">Every day at 9:00</option><option value="weekdays">Weekdays at 9:00</option><option value="weekly">Mondays at 9:00</option><option value="custom">Custom (cron)</option></select></div>
      <div class="field"><label>Cron</label><input type="text" class="cr" spellcheck="false" placeholder="m h dom mon dow"></div></div>
      <div class="help nx" style="margin:-8px 0 12px"></div>
      <div class="field"><label>Without asking, Anvil may</label><select class="md"><option value="ask">Nothing: wait for my approval here</option><option value="edits">Edit files in the folder</option><option value="auto">Edit files and run commands (destructive ones still ask)</option></select></div>
      <div style="display:flex;gap:8px;justify-content:flex-end"></div>`;
    const $f = (c) => f.querySelector(c);
    $f(".nm").value = s?.name || ""; $f(".pr").value = s?.prompt || ""; $f(".rt").value = s?.root || state.meta?.root || data.default_root;
    $f(".cr").value = s?.cron || data.presets.daily; $f(".when").value = presetOf($f(".cr").value); $f(".md").value = s?.mode || "edits";
    const preview = async () => { const r = await api("/api/schedules/preview", { method: "POST", body: { cron: $f(".cr").value } }); $f(".nx").textContent = r.error ? r.error : `${r.description} · next run ${fmtWhen(r.next_run)}`; $f(".nx").style.color = r.error ? "var(--err)" : ""; };
    $f(".when").onchange = () => { const v = $f(".when").value; if (v !== "custom") $f(".cr").value = data.presets[v]; else $f(".cr").focus(); preview(); };
    let t; $f(".cr").oninput = () => { $f(".when").value = presetOf($f(".cr").value); clearTimeout(t); t = setTimeout(preview, 250); };
    if ($f(".pick")) $f(".pick").onclick = async () => { const d = await api("/api/pick-folder", { method: "POST" }); if (d.path) $f(".rt").value = d.path; };
    f.lastElementChild.append(btn("Cancel", "", () => { editing = null; render(); }), btn(s ? "Save" : "Create task", "primary", async () => {
      await act(api("/api/schedules", { method: "POST", body: { id: s?.id, name: $f(".nm").value, prompt: $f(".pr").value, root: $f(".rt").value, cron: $f(".cr").value, mode: $f(".md").value, enabled: s ? s.enabled : true } }), s ? "Saved" : "Scheduled");
      editing = null; load();
    }));
    preview(); setTimeout(() => $f(s ? ".pr" : ".nm").focus(), 30);
    return f;
  }
  PANELS.scheduled._refresh = () => { if (!editing) load(); };
  await load();
};

// ---------- browser ----------
PANELS.browser = async function () {
  shell("browser", "Browser", "globe");
  async function load() {
    const b = await api("/api/browser");
    body.innerHTML = `<p class="x-sub">With the browser on, Anvil can open pages, click, type and take screenshots in its own Chromium window (separate profile, logins persist). It asks before clicking or typing unless you allow it.</p>`;
    const r = el("div", "x-row");
    r.innerHTML = `<span class="x-ico">${ico("globe", 14)}</span><div class="main"><div class="t"><span class="x-dot ${b.installed && b.enabled ? "ready" : b.installed ? "" : "error"}"></span>Browser tools</div><div class="d"></div></div><div class="acts"></div>`;
    r.querySelector(".d").textContent = !b.installed ? "Playwright isn't installed yet." : b.enabled ? "Available to new sessions." : "Turned off.";
    const acts = r.querySelector(".acts");
    if (!b.installed) acts.appendChild(btn(b.installing ? "Installing…" : "Install", "primary", async () => { await act(api("/api/browser/install", { method: "POST" })); load(); }));
    else {
      acts.appendChild(btn("Test", "", async (e) => { e.target.textContent = "Testing…"; const x = await api("/api/browser/test", { method: "POST" }); toast(x.error || `Works: ${x.result}`); e.target.textContent = "Test"; }));
      acts.appendChild(sw(b.enabled, (on) => act(api("/api/browser", { method: "POST", body: { enabled: on } })).then(load)));
    }
    body.appendChild(r);
    if (b.installed) {
      const f = el("div", "x-form");
      f.innerHTML = `<div class="row"><div class="field"><label>Window</label><select class="hl"><option value="auto">Visible when there's a display</option><option value="false">Always visible</option><option value="true">Hidden (headless)</option></select></div>
        <div class="field"><label>Browser</label><select class="ch"><option value="auto">Automatic</option><option value="chromium">Chromium</option><option value="chrome">Google Chrome</option><option value="msedge">Microsoft Edge</option></select></div></div><div class="help">Changes apply to new sessions.</div>`;
      f.querySelector(".hl").value = b.headless; f.querySelector(".ch").value = b.channel;
      f.querySelectorAll("select").forEach((s) => s.onchange = () => act(api("/api/browser", { method: "POST", body: { headless: f.querySelector(".hl").value, channel: f.querySelector(".ch").value } }), "Saved"));
      body.appendChild(f);
    }
    if (b.log) { const l = el("div", "x-log"); l.textContent = b.log; body.appendChild(l); l.scrollTop = l.scrollHeight; }
  }
  PANELS.browser._refresh = load;
  await load();
};

// ---------- live updates ----------
function xToast(text, label, fn) {
  document.querySelector(".x-toast")?.remove();
  const t = el("div", "x-toast"); t.appendChild(el("span")).textContent = text;
  if (label) t.appendChild(btn(label, "primary", () => { t.remove(); fn(); }));
  t.appendChild(btn("✕", "", () => t.remove()));
  document.body.appendChild(t); setTimeout(() => t.remove(), 9000);
}
async function counts() {
  try {
    const [a, c, s] = await Promise.all([api("/api/artifacts"), api("/api/connectors"), api("/api/schedules")]);
    const set = (id, n) => { const b = nav.querySelector(`[data-panel=${id}] .n`); if (b) { b.textContent = n; b.hidden = !n; } };
    set("artifacts", a.artifacts.length); set("connectors", c.connectors.filter((x) => x.enabled).length); set("scheduled", s.schedules.filter((x) => x.enabled).length);
  } catch {}
}
const baseOnEvent = window.onEvent;
window.onEvent = function (ev) {
  switch (ev.type) {
    case "connectors_changed": if (current === "connectors") refresh(); counts(); return;
    case "artifacts_changed": if (current === "artifacts") refresh(); else xToast(`Artifact saved: ${ev.title}`, "Open", () => open("artifacts", ev.id)); counts(); return;
    case "sessions_changed": loadState(); return;
    case "browser_changed": if (current === "browser") refresh(); return;
  }
  return baseOnEvent(ev);
};
window.anvilExtras = { open };
if (TOKEN) counts();
})();
