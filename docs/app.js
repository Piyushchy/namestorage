"use strict";
const cfg = window.NAMESTORAGE_CONFIG;
const $ = (id) => document.getElementById(id);
const CH = 16 * 1024;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let credential = null, pc = null, dc = null, nextId = 1, curPath = "/", dl = null, busy = false;
const calls = new Map();

const setStatus = (t) => { $("status").textContent = t; };

/* ---------- Google sign-in ---------- */
function initGoogle() {
  if (!(window.google && google.accounts && google.accounts.id)) return setTimeout(initGoogle, 200);
  google.accounts.id.initialize({
    client_id: cfg.googleClientId,
    callback: (r) => { credential = r.credential; $("connect").hidden = false; setStatus("Signed in. Click Connect."); },
  });
  google.accounts.id.renderButton($("gbtn"), { theme: "outline", size: "large" });
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

async function connect() {
  $("connect").disabled = true;
  setStatus("Connecting…");
  try {
    pc = new RTCPeerConnection({ iceServers: cfg.iceServers });
    dc = pc.createDataChannel("files");
    dc.binaryType = "arraybuffer";
    dc.onmessage = onData;
    dc.onclose = () => { setStatus("Disconnected. Sign in again to reconnect."); $("files").hidden = true; $("login").hidden = false; $("connect").disabled = false; credential = null; $("connect").hidden = true; };
    const opened = new Promise((res, rej) => {
      dc.onopen = res;
      setTimeout(() => rej(new Error("Could not open a direct connection (network may block peer-to-peer)")), 45000);
    });
    opened.catch(() => {});
    await pc.setLocalDescription(await pc.createOffer());
    await gathered(pc);
    const answer = await signal(pc.localDescription.sdp);
    await pc.setRemoteDescription({ type: "answer", sdp: answer });
    await opened;
    $("login").hidden = true;
    $("files").hidden = false;
    setStatus("Connected directly to your laptop.");
    await refresh("/");
  } catch (e) {
    setStatus("Error: " + e.message);
    $("connect").disabled = false;
    if (pc) pc.close();
  }
}
$("connect").addEventListener("click", connect);

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
    const id = nextId++;
    calls.set(id, { resolve, reject });
    dc.send(JSON.stringify({ id, op, ...args }));
  });
}

/* ---------- file list ---------- */
const fmt = (n) => { const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; while (n >= 1024 && i < 4) { n /= 1024; i++; } return (i ? n.toFixed(1) : n) + " " + u[i]; };
const join = (dir, name) => (dir === "/" ? "" : dir) + "/" + name;

async function refresh(path) {
  const r = await call("list", { path });
  curPath = path;
  renderCrumbs();
  const rows = $("rows");
  rows.replaceChildren();
  if (path !== "/") rows.append(row({ name: "..", dir: true, size: 0 }, true));
  r.entries.forEach((e) => rows.append(row(e, false)));
}

function renderCrumbs() {
  const box = $("crumbs");
  box.replaceChildren();
  const parts = curPath.split("/").filter(Boolean);
  let acc = "";
  const add = (label, p) => {
    const b = document.createElement("button"); b.className = "link"; b.textContent = label;
    b.onclick = () => guarded(() => refresh(p)); box.append(b);
  };
  add("Home", "/");
  parts.forEach((p) => { acc += "/" + p; box.append(" / "); add(p, acc); });
}

function row(e, isUp) {
  const tr = document.createElement("tr");
  const name = document.createElement("td");
  if (e.dir) {
    const b = document.createElement("button"); b.className = "link"; b.textContent = "📁 " + e.name;
    b.onclick = () => guarded(() => refresh(isUp ? curPath.replace(/\/[^/]+$/, "") || "/" : join(curPath, e.name)));
    name.append(b);
  } else { name.textContent = e.name; }
  const size = document.createElement("td"); size.className = "n"; size.textContent = e.dir ? "" : fmt(e.size);
  const act = document.createElement("td"); act.className = "n";
  if (!e.dir) { const b = document.createElement("button"); b.textContent = "Download"; b.onclick = () => guarded(() => download(join(curPath, e.name), e.name)); act.append(b); }
  tr.append(name, size, act);
  return tr;
}

async function guarded(fn) {
  if (busy) return setStatus("Wait for the current transfer to finish.");
  try { await fn(); } catch (e) { setStatus("Error: " + e.message); }
}

/* ---------- transfers ---------- */
function progress(done, total) { const p = $("prog"); p.hidden = !total; p.max = total || 1; p.value = done; }

async function download(path, name) {
  busy = true; $("cancel").hidden = false;
  let writable = null; const parts = [];
  try {
    if (window.showSaveFilePicker) writable = await (await showSaveFilePicker({ suggestedName: name })).createWritable();
    const first = await call("download", { path });
    let got = 0;
    const finished = new Promise((res) => {
      dl = { id: first.id, finish: res, chunk(b) { got += b.byteLength; writable ? writable.write(b) : parts.push(b); progress(got, first.size); } };
    });
    const end = await finished;
    dl = null;
    if (end.cancelled) { if (writable) await writable.abort(); setStatus("Download cancelled."); return; }
    if (got !== first.size) throw new Error("Download incomplete");
    if (writable) await writable.close();
    else {
      const a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob(parts)); a.download = name; a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 60000);
    }
    setStatus("Downloaded " + name);
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
    progress(off, file.size);
  }
  await call("upload_end");
}

async function uploadMany(files) {
  busy = true;
  try {
    for (const f of files) { setStatus("Uploading " + f.name + "…"); await upload(f); }
    setStatus("Upload finished.");
    await refresh(curPath);
  } finally { busy = false; progress(0, 0); }
}

$("up").onclick = () => $("picker").click();
$("picker").onchange = (e) => { const f = [...e.target.files]; e.target.value = ""; if (f.length) guarded(() => uploadMany(f)); };
$("mk").onclick = () => guarded(async () => {
  const name = prompt("Folder name:");
  if (name) { await call("mkdir", { path: curPath, name }); await refresh(curPath); }
});
$("cancel").onclick = () => { if (dc && dc.readyState === "open") dc.send(JSON.stringify({ op: "cancel" })); };
const drop = $("drop");
drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
drop.ondragleave = () => drop.classList.remove("over");
drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); const f = [...e.dataTransfer.files]; if (f.length) guarded(() => uploadMany(f)); };
