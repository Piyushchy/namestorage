"use strict";
const cfg = window.NAMESTORAGE_CONFIG;
const $ = (id) => document.getElementById(id);
const CH = 16 * 1024;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let credential = null, pc = null, dc = null, nextId = 1, curPath = "/", dl = null, busy = false;
let entries = [], caps = { upload: false, delete: false }, me = "";
const sort = { key: "name", dir: 1 };
const calls = new Map();

/* ---------- icons (constant strings, never built from file names) ---------- */
const svg = (d) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
const FOLDER = '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>';
const I = {
  drive: svg('<rect x="2.5" y="5" width="19" height="14" rx="4"/><circle cx="7.5" cy="12" r="1.2" fill="currentColor"/><path d="M12 12h6"/>'),
  folder: svg(FOLDER),
  folderPlus: svg(FOLDER + '<path d="M12 11v5M9.5 13.5h5"/>'),
  file: svg('<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>'),
  up: svg('<path d="M15 18l-6-6 6-6"/>'),
  upload: svg('<path d="M12 16V5"/><path d="m7 9 5-5 5 5"/><path d="M5 20h14"/>'),
  download: svg('<path d="M12 4v11"/><path d="m7 11 5 5 5-5"/><path d="M5 20h14"/>'),
  trash: svg('<path d="M4 7h16"/><path d="M10 11v6M14 11v6"/><path d="M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12"/><path d="M9 7V4h6v3"/>'),
  link: svg('<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>'),
};
document.querySelectorAll("[data-i]").forEach((el) => { el.innerHTML = I[el.dataset.i]; });

/* ---------- status and messages ---------- */
function setState(text, kind) {
  $("state").textContent = text;
  $("pill").dataset.kind = kind;
}
function say(text, kind) {
  const m = $("msg");
  if (!text) { m.hidden = true; return; }
  m.textContent = text;
  m.className = "msg" + (kind ? " " + kind : "");
  m.hidden = false;
}

/* ---------- Google sign-in ---------- */
function initGoogle() {
  if (!(window.google && google.accounts && google.accounts.id)) return setTimeout(initGoogle, 200);
  google.accounts.id.initialize({
    client_id: cfg.googleClientId,
    callback: (r) => { credential = r.credential; connect(); },
  });
  google.accounts.id.renderButton($("gbtn"), { theme: "outline", size: "large", shape: "rectangular" });
}
window.addEventListener("load", initGoogle);

/* ---------- connection ---------- */
function gathered(p) {
  return new Promise((res) => {
    if (p.iceGatheringState === "complete") return res();
    const t = setTimeout(res, 6000);
    p.addEventListener("icegatheringstatechange", () => {
      if (p.iceGatheringState === "complete") { clearTimeout(t); res(); }
    });
  });
}

function signal(sdp) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(cfg.signalingUrl);
    const t = setTimeout(() => { ws.close(); reject(new Error("Signaling timed out")); }, 40000);
    ws.onopen = () => ws.send(JSON.stringify({ type: "offer", token: credential, sdp }));
    ws.onmessage = (ev) => {
      clearTimeout(t); ws.close();
      const m = JSON.parse(ev.data);
      m.type === "answer" ? resolve(m.sdp) : reject(new Error(m.error || "Refused"));
    };
    ws.onerror = () => { clearTimeout(t); reject(new Error("Cannot reach the signaling server")); };
  });
}

function showLogin() {
  $("files").hidden = true;
  $("login").hidden = false;
  $("disconnect").hidden = true;
  $("who").hidden = true;
}

function dropSession(message) {
  credential = null;
  entries = [];
  busy = false;
  showLogin();
  $("connect").hidden = true;
  setState("Signed out", "idle");
  say(message, message ? "" : undefined);
  if (window.google && google.accounts && google.accounts.id) google.accounts.id.disableAutoSelect();
}

async function connect() {
  $("connect").hidden = true;
  say("");
  setState("Connecting…", "busy");
  try {
    pc = new RTCPeerConnection({ iceServers: cfg.iceServers });
    dc = pc.createDataChannel("files");
    dc.binaryType = "arraybuffer";
    dc.onmessage = onData;
    dc.onclose = () => dropSession("Disconnected. Sign in again to reconnect.");
    const opened = new Promise((res, rej) => {
      dc.onopen = res;
      setTimeout(() => rej(new Error("Could not open a direct connection (your network may block peer-to-peer)")), 45000);
    });
    opened.catch(() => {});
    await pc.setLocalDescription(await pc.createOffer());
    await gathered(pc);
    const answer = await signal(pc.localDescription.sdp);
    await pc.setRemoteDescription({ type: "answer", sdp: answer });
    await opened;
    $("login").hidden = true;
    $("files").hidden = false;
    $("disconnect").hidden = false;
    setState("Connected directly", "ok");
    await refresh("/");
  } catch (e) {
    setState("Not connected", "err");
    say(e.message, "err");
    $("connect").hidden = false;
    if (pc) pc.close();
  }
}
$("connect").addEventListener("click", connect);
$("disconnect").addEventListener("click", () => {
  const ch = dc, p = pc;
  if (ch) ch.onclose = null;
  dc = null;
  dropSession("Disconnected.");
  if (ch) ch.close();
  if (p) p.close();
});

/* ---------- protocol ---------- */
function onData(ev) {
  if (typeof ev.data !== "string") { if (dl) dl.chunk(ev.data); return; }
  const m = JSON.parse(ev.data);
  if (m.done && dl && dl.id === m.id) { dl.finish(m); return; }
  const c = calls.get(m.id);
  if (!c) return;
  calls.delete(m.id);
  m.ok ? c.resolve(m) : c.reject(new Error(m.error || "failed"));
}

function call(op, args = {}) {
  return new Promise((resolve, reject) => {
    if (!dc || dc.readyState !== "open") return reject(new Error("Not connected"));
    const id = nextId++;
    calls.set(id, { resolve, reject });
    dc.send(JSON.stringify({ id, op, ...args }));
  });
}

/* ---------- formatting ---------- */
const fmtSize = (n) => { const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; while (n >= 1024 && i < 4) { n /= 1024; i++; } return (i ? n.toFixed(1) : n) + " " + u[i]; };
const dtf = new Intl.DateTimeFormat(undefined, { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
const fmtDate = (s) => dtf.format(new Date(s * 1000));
const fullDate = (s) => new Date(s * 1000).toLocaleString(undefined, { dateStyle: "full", timeStyle: "medium" });
const join = (dir, name) => (dir === "/" ? "" : dir) + "/" + name;
const when = (e) => (e.at != null ? e.at : e.mtime);

/* ---------- file list ---------- */
async function refresh(path) {
  const r = await call("list", { path });
  curPath = path;
  entries = r.entries;
  caps = r.caps || caps;
  me = r.email || me;
  $("who").textContent = me;
  $("who").hidden = !me;
  $("up").hidden = !caps.upload;
  $("mk").hidden = !caps.upload;
  render();
}

function renderCrumbs() {
  const box = $("crumbs");
  box.replaceChildren();
  const parts = curPath.split("/").filter(Boolean);
  const add = (label, p, current) => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = label;
    if (current) { b.className = "cur"; b.setAttribute("aria-current", "page"); }
    else b.onclick = () => guarded(() => refresh(p));
    box.append(b);
  };
  let acc = "";
  add("Home", "/", parts.length === 0);
  parts.forEach((p, i) => {
    acc += "/" + p;
    const sep = document.createElement("span");
    sep.className = "sep"; sep.textContent = "/";
    box.append(sep);
    add(p, acc, i === parts.length - 1);
  });
}

function compare(a, b) {
  if (a.dir !== b.dir) return a.dir ? -1 : 1;
  let r;
  if (sort.key === "size") r = a.size - b.size;
  else if (sort.key === "date") r = when(a) - when(b);
  else r = a.name.localeCompare(b.name, undefined, { sensitivity: "base", numeric: true });
  return (r || a.name.localeCompare(b.name)) * sort.dir;
}

function render() {
  renderCrumbs();
  document.querySelectorAll("th .sort").forEach((b) => {
    const th = b.closest("th");
    if (b.dataset.sort === sort.key) th.setAttribute("aria-sort", sort.dir === 1 ? "ascending" : "descending");
    else th.removeAttribute("aria-sort");
  });
  const rows = $("rows");
  rows.replaceChildren();
  if (curPath !== "/") rows.append(row({ name: "Parent folder", dir: true, size: 0 }, true));
  [...entries].sort(compare).forEach((e) => rows.append(row(e, false)));
  $("empty").hidden = entries.length > 0;
  $("tbl").hidden = entries.length === 0 && curPath === "/";
}

function iconButton(icon, label, cls, onclick) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "icon " + cls;
  b.innerHTML = icon;
  b.title = label;
  b.setAttribute("aria-label", label);
  if (onclick) b.onclick = onclick;
  return b;
}

function row(e, isUp) {
  const tr = document.createElement("tr");

  const nameCell = document.createElement("td");
  const wrap = document.createElement("div");
  wrap.className = "name";
  const fi = document.createElement("span");
  fi.className = "fi" + (e.dir ? " dir" : "");
  fi.innerHTML = isUp ? I.up : e.dir ? I.folder : I.file;
  let label;
  if (e.dir) {
    label = document.createElement("button");
    label.type = "button";
    label.onclick = () => guarded(() => refresh(isUp ? curPath.replace(/\/[^/]+$/, "") || "/" : join(curPath, e.name)));
  } else {
    label = document.createElement("span");
  }
  label.className = "nm";
  label.textContent = e.name;
  label.title = e.name;
  wrap.append(fi, label);
  nameCell.append(wrap);

  if (isUp) {
    tr.append(nameCell);
    for (let i = 0; i < 4; i++) tr.append(document.createElement("td"));
    return tr;
  }

  const recorded = e.at != null;
  const size = e.dir ? "" : fmtSize(e.size);
  const dateText = fmtDate(when(e));
  const sub = document.createElement("div");
  sub.className = "sub";
  sub.textContent = [size, (recorded ? "" : "modified ") + dateText, e.by].filter(Boolean).join(" · ");
  nameCell.append(sub);

  const sizeCell = document.createElement("td");
  sizeCell.className = "n num";
  sizeCell.textContent = size;

  const dateCell = document.createElement("td");
  dateCell.className = "dt" + (recorded ? "" : " mod");
  dateCell.textContent = dateText;
  dateCell.title = recorded ? "Uploaded " + fullDate(e.at)
                            : "Last modified " + fullDate(e.mtime) + " (not uploaded through this site)";

  const byCell = document.createElement("td");
  byCell.className = "by";
  byCell.textContent = e.by || "—";
  if (e.by) byCell.title = "Uploaded by " + e.by;

  const act = document.createElement("td");
  const box = document.createElement("div");
  box.className = "rowact";
  // Share-by-link is not built yet: the symbol is shown, deliberately inert.
  const share = iconButton(I.link, "Share link (coming soon)", "soon", null);
  share.setAttribute("aria-disabled", "true");
  box.append(share);
  if (!e.dir) box.append(iconButton(I.download, "Download " + e.name, "", () => guarded(() => download(join(curPath, e.name), e.name))));
  if (caps.delete) box.append(iconButton(I.trash, "Delete " + e.name, "del", () => guarded(() => remove(e))));
  act.append(box);

  tr.append(nameCell, sizeCell, dateCell, byCell, act);
  return tr;
}

document.querySelectorAll("th .sort").forEach((b) => {
  b.addEventListener("click", () => {
    const k = b.dataset.sort;
    if (sort.key === k) sort.dir = -sort.dir; else { sort.key = k; sort.dir = k === "name" ? 1 : -1; }
    render();
  });
});

async function guarded(fn) {
  if (busy) return say("Wait for the current transfer to finish.");
  try { await fn(); } catch (e) { say(e.message, "err"); }
}

/* ---------- delete ---------- */
function confirmDelete(e) {
  const d = $("confirm");
  $("ctitle").textContent = e.dir ? "Delete this folder?" : "Delete this file?";
  const p = $("cbody");
  p.replaceChildren();
  const b = document.createElement("b");
  b.textContent = e.name;
  p.append(b, document.createTextNode(e.dir
    ? " will be removed from your laptop. Only empty folders can be deleted."
    : " will be permanently deleted from your laptop. This cannot be undone."));
  if (typeof d.showModal !== "function") return Promise.resolve(window.confirm("Delete " + e.name + "?"));
  return new Promise((resolve) => {
    const done = (v) => { d.close(); $("cyes").onclick = $("cno").onclick = null; resolve(v); };
    $("cyes").onclick = () => done(true);
    $("cno").onclick = () => done(false);
    d.oncancel = () => resolve(false);
    d.showModal();
    $("cno").focus();
  });
}

async function remove(e) {
  if (!(await confirmDelete(e))) return;
  await call("delete", { path: curPath, name: e.name });
  say("Deleted " + e.name, "ok");
  await refresh(curPath);
}

/* ---------- transfers ---------- */
function progress(done, total, name) {
  const x = $("xfer");
  if (!total && !name) { x.hidden = true; return; }
  x.hidden = false;
  $("xname").textContent = name || "";
  $("prog").max = total || 1;
  $("prog").value = done;
  $("xpct").textContent = total ? Math.floor((done / total) * 100) + "%" : "";
}

async function download(path, name) {
  busy = true; $("cancel").hidden = false; say("");
  let writable = null; const parts = [];
  progress(0, 0, "Downloading " + name);
  try {
    if (window.showSaveFilePicker) writable = await (await showSaveFilePicker({ suggestedName: name })).createWritable();
    const first = await call("download", { path });
    let got = 0;
    const finished = new Promise((res) => {
      dl = { id: first.id, finish: res, chunk(b) { got += b.byteLength; writable ? writable.write(b) : parts.push(b); progress(got, first.size, "Downloading " + name); } };
    });
    const end = await finished;
    dl = null;
    if (end.cancelled) { if (writable) await writable.abort(); say("Download cancelled."); return; }
    if (got !== first.size) throw new Error("Download incomplete");
    if (writable) await writable.close();
    else {
      const a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob(parts)); a.download = name; a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 60000);
    }
    say("Downloaded " + name, "ok");
  } finally { dl = null; busy = false; $("cancel").hidden = true; progress(0, 0); }
}

async function upload(file) {
  await call("upload_start", { path: curPath, name: file.name, size: file.size });
  let off = 0;
  while (off < file.size) {
    const buf = await file.slice(off, off + (1 << 20)).arrayBuffer();
    for (let i = 0; i < buf.byteLength; i += CH) {
      while (dc.bufferedAmount > (1 << 20)) await sleep(10);
      dc.send(buf.slice(i, i + CH));
    }
    off += buf.byteLength;
    progress(off, file.size, "Uploading " + file.name);
  }
  await call("upload_end");
}

async function uploadMany(files) {
  const taken = new Set(entries.map((e) => e.name));
  const clash = files.find((f) => taken.has(f.name));
  if (clash) return say(`"${clash.name}" already exists here. Uploads never overwrite; rename it or delete the old one first.`, "err");
  busy = true; say("");
  try {
    for (const f of files) { progress(0, f.size, "Uploading " + f.name); await upload(f); }
    say(files.length === 1 ? "Uploaded " + files[0].name : "Uploaded " + files.length + " files", "ok");
    await refresh(curPath);
  } finally { busy = false; progress(0, 0); }
}

$("up").onclick = () => $("picker").click();
$("picker").onchange = (e) => { const f = [...e.target.files]; e.target.value = ""; if (f.length) guarded(() => uploadMany(f)); };
$("mk").onclick = () => guarded(async () => {
  const name = prompt("Folder name:");
  if (name) { await call("mkdir", { path: curPath, name }); say("Created folder " + name, "ok"); await refresh(curPath); }
});
$("cancel").onclick = () => { if (dc && dc.readyState === "open") dc.send(JSON.stringify({ op: "cancel" })); };

const drop = $("drop");
drop.ondragover = (e) => { e.preventDefault(); if (caps.upload) drop.classList.add("over"); };
drop.ondragleave = (e) => { if (!drop.contains(e.relatedTarget)) drop.classList.remove("over"); };
drop.ondrop = (e) => {
  e.preventDefault(); drop.classList.remove("over");
  const f = [...e.dataTransfer.files];
  if (f.length && caps.upload) guarded(() => uploadMany(f));
};
