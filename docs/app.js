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
    // Closing the connection fires the data channel's close handler, which would replace this
    // error with a generic "Disconnected" and hide the retry button. Detach it first.
    if (dc) dc.onclose = null;
    if (pc) pc.close();
    pc = dc = null;
    setState("Not connected", "err");
    say(e.message, "err");
    $("connect").hidden = false;
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

let stop = false;  // set by Cancel; checked between chunks

async function upload(file) {
  await call("upload_start", { path: curPath, name: file.name, size: file.size });
  try {
    let off = 0;
    while (off < file.size) {
      const buf = await file.slice(off, off + (1 << 20)).arrayBuffer();
      for (let i = 0; i < buf.byteLength; i += CH) {
        while (dc.bufferedAmount > (1 << 20) && !stop) await sleep(10);
        if (stop) throw new Error("cancelled");
        dc.send(buf.slice(i, i + CH));
      }
      off += buf.byteLength;
      progress(off, file.size, "Uploading " + file.name);
    }
    await call("upload_end");
  } catch (e) {
    // Tell the laptop to throw the partial file away so the name is free again.
    if (dc && dc.readyState === "open") dc.send(JSON.stringify({ id: nextId++, op: "upload_abort" }));
    throw e;
  }
}

const list = (names) => names.length > 3 ? names.slice(0, 3).join(", ") + " and " + (names.length - 3) + " more" : names.join(", ");

/* One bad file never stops the rest: each is tried, then the outcome is summarised. */
async function uploadMany(files, skipped = []) {
  const taken = new Set(entries.map((e) => e.name));
  const failed = skipped.slice(), todo = [];
  for (const f of files) {
    if (taken.has(f.name)) failed.push(f.name + " (already exists)");
    else { taken.add(f.name); todo.push(f); }
  }
  busy = true; stop = false; $("cancel").hidden = false; say("");
  let ok = 0, cancelled = false;
  try {
    for (let n = 0; n < todo.length; n++) {
      const f = todo[n], tag = todo.length > 1 ? `Uploading ${n + 1} of ${todo.length}: ` : "Uploading ";
      progress(0, f.size, tag + f.name);
      try { await upload(f); ok++; }
      catch (e) {
        if (stop) { cancelled = true; break; }
        failed.push(f.name + " (" + e.message + ")");
        if (!dc || dc.readyState !== "open") break;
      }
    }
  } finally { busy = false; stop = false; $("cancel").hidden = true; progress(0, 0); }
  if (dc && dc.readyState === "open") await refresh(curPath);
  const head = ok === 1 && !failed.length && todo.length === 1 ? "Uploaded " + todo[0].name : ok ? "Uploaded " + ok + (ok === 1 ? " file" : " files") : "";
  if (cancelled) say((head ? head + ". " : "") + "Upload cancelled.", ok ? "ok" : "");
  else if (failed.length) say((head ? head + ". " : "") + "Not uploaded: " + list(failed), "err");
  else say(head, "ok");
}

$("up").onclick = () => $("picker").click();
$("picker").onchange = (e) => { const f = [...e.target.files]; e.target.value = ""; if (f.length) guarded(() => uploadMany(f)); };
$("mk").onclick = () => guarded(async () => {
  const name = prompt("Folder name:");
  if (name) { await call("mkdir", { path: curPath, name }); say("Created folder " + name, "ok"); await refresh(curPath); }
});
$("cancel").onclick = () => {
  if (dl) { if (dc && dc.readyState === "open") dc.send(JSON.stringify({ op: "cancel" })); }
  else stop = true;
};

/* ---------- drag and drop: the whole page is the drop target ---------- */
const veil = $("dropveil");
let depth = 0;
const hasFiles = (e) => e.dataTransfer && [...(e.dataTransfer.types || [])].includes("Files");

function dropState() {
  if (!dc || dc.readyState !== "open") return { ok: false, text: "Sign in to upload files" };
  if (!caps.upload) return { ok: false, text: "Uploads are turned off on this drive" };
  if (busy) return { ok: false, text: "Wait for the current transfer to finish" };
  return { ok: true, text: "Drop to upload to " + (curPath === "/" ? "the top folder" : curPath) };
}
function showVeil() {
  const s = dropState();
  veil.dataset.state = s.ok ? "valid" : "blocked";
  $("dropmsg").textContent = s.text;
  veil.hidden = false;
}
function hideVeil() { depth = 0; veil.hidden = true; }

document.addEventListener("dragenter", (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault(); depth++; showVeil();
});
document.addEventListener("dragover", (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault();  // without this the browser would open the file instead
  e.dataTransfer.dropEffect = dropState().ok ? "copy" : "none";
});
document.addEventListener("dragleave", (e) => {
  if (!hasFiles(e)) return;
  if (--depth <= 0) hideVeil();
});
document.addEventListener("drop", (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault(); hideVeil();
  const s = dropState();
  if (!s.ok) return say(s.text + ".", "err");
  // Read everything now: the browser empties the drop data once this handler returns.
  const files = [], folders = [];
  const items = [...(e.dataTransfer.items || [])].filter((i) => i.kind === "file");
  if (items.length) {
    for (const it of items) {
      const ent = it.webkitGetAsEntry && it.webkitGetAsEntry();
      if (ent && ent.isDirectory) folders.push(ent.name + " (folders cannot be uploaded)");
      else { const f = it.getAsFile(); if (f) files.push(f); }
    }
  } else files.push(...e.dataTransfer.files);
  if (files.length || folders.length) guarded(() => uploadMany(files, folders));
});
