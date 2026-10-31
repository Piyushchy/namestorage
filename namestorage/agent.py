"""Laptop agent: serves ONE folder to approved browsers over WebRTC data channels.

Run:  NAMESTORAGE_SECRET=... python -m namestorage.agent /etc/namestorage/agent.json
The agent only makes outbound connections (to the signaling server), so no port
needs to be opened at home and CGNAT is fine.
"""
import argparse
import asyncio
import json
import logging
import os
import secrets
import shutil
import time

from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
from websockets.asyncio.client import connect

from .paths import META_NAME, PathError, resolve, valid_name
from .ticket import TicketError, verify_ticket

CHUNK = 16 * 1024
HIGH_WATER = 1024 * 1024
MAX_LIST = 5000
log = logging.getLogger("namestorage.agent")


class Session:
    def __init__(self, agent, email, channel):
        self.agent, self.email, self.channel = agent, email, channel
        self.root = agent.root
        self.cfg = agent.cfg
        self.q = asyncio.Queue()
        self.upload = None
        self.cancel = False
        self.closed = False

    # --- called synchronously by aiortc for every incoming message --------------------------
    def on_message(self, m):
        if isinstance(m, str) and '"cancel"' in m and len(m) < 200:
            try:
                if json.loads(m).get("op") == "cancel":
                    self.cancel = True
                    return
            except ValueError:
                pass
        if self.q.qsize() > 1000:
            log.warning("%s: flooding, closing", self.email)
            self.close()
            return
        self.q.put_nowait(m)

    def send(self, obj):
        if self.channel.readyState == "open":
            self.channel.send(json.dumps(obj))

    def close(self):
        if not self.closed:
            self.closed = True
            self.abort_upload()
            self.q.put_nowait(None)

    async def run(self):
        while True:
            m = await self.q.get()
            if m is None:
                break
            try:
                await self.handle(m)
            except PathError:
                self.reply_err(m, "invalid path")
            except OSError as e:
                log.warning("%s: I/O error: %s", self.email, e)
                self.reply_err(m, "storage error")
            except Exception:
                log.exception("%s: unexpected error", self.email)
                self.reply_err(m, "internal error")
        self.abort_upload()

    def reply_err(self, m, error):
        mid = None
        if isinstance(m, str):
            try:
                mid = json.loads(m).get("id")
            except ValueError:
                pass
        self.send({"id": mid, "ok": False, "error": error})

    async def handle(self, m):
        if isinstance(m, bytes):
            return self.on_chunk(m)
        if len(m) > 65536:
            return self.reply_err(m, "message too large")
        req = json.loads(m)
        op, rid = req.get("op"), req.get("id")
        if op == "list":
            return self.op_list(rid, req.get("path", "/"))
        if op == "download":
            return await self.op_download(rid, req.get("path"))
        if op == "upload_start":
            return self.op_upload_start(rid, req)
        if op == "upload_end":
            return self.op_upload_end(rid)
        if op == "mkdir":
            return self.op_mkdir(rid, req)
        self.send({"id": rid, "ok": False, "error": "unknown operation"})

    # --- operations -------------------------------------------------------------------------
    def op_list(self, rid, path):
        d = resolve(self.root, path)
        if not os.path.isdir(d):
            return self.send({"id": rid, "ok": False, "error": "not a folder"})
        entries = []
        with os.scandir(d) as it:
            for e in it:
                if e.is_symlink() or e.name.endswith(".nspart") or e.name == META_NAME:
                    continue
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                is_dir = e.is_dir(follow_symlinks=False)
                item = {"name": e.name, "dir": is_dir, "size": st.st_size, "mtime": int(st.st_mtime)}
                rec = self.agent.record_for(os.path.join(d, e.name), st, is_dir)
                if rec:
                    item["by"], item["at"] = rec["by"], rec["at"]
                entries.append(item)
                if len(entries) >= MAX_LIST:
                    break
        entries.sort(key=lambda x: (not x["dir"], x["name"].lower()))
        self.send({"id": rid, "ok": True, "entries": entries, "email": self.email,
                   "caps": {"upload": bool(self.cfg.get("allow_upload", False))}})

    async def op_download(self, rid, path):
        target = resolve(self.root, path)
        if not os.path.isfile(target):
            return self.send({"id": rid, "ok": False, "error": "not a file"})
        log.info("%s download /%s", self.email, os.path.relpath(target, self.root))
        self.cancel = False
        self.send({"id": rid, "ok": True, "size": os.path.getsize(target)})
        sent = 0
        with open(target, "rb") as f:
            while not (self.cancel or self.closed):
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                while self.channel.bufferedAmount > HIGH_WATER and not self.closed:
                    await asyncio.sleep(0.01)
                if self.channel.readyState != "open":
                    return
                self.channel.send(chunk)
                sent += len(chunk)
                await asyncio.sleep(0)
        self.send({"id": rid, "done": True, "sent": sent, "cancelled": self.cancel})

    def op_upload_start(self, rid, req):
        if not self.cfg.get("allow_upload", False):
            return self.send({"id": rid, "ok": False, "error": "uploads are disabled"})
        if self.upload:
            return self.send({"id": rid, "ok": False, "error": "another upload is running"})
        name, size = req.get("name"), req.get("size")
        if not valid_name(name) or not isinstance(size, int) or isinstance(size, bool) or size < 0:
            return self.send({"id": rid, "ok": False, "error": "bad file name or size"})
        parent = resolve(self.root, req.get("path", "/"))
        if not os.path.isdir(parent):
            return self.send({"id": rid, "ok": False, "error": "not a folder"})
        final = os.path.join(parent, name)
        if os.path.lexists(final):
            return self.send({"id": rid, "ok": False, "error": "a file with that name already exists"})
        if size + self.cfg.get("min_free_bytes", 1 << 30) > shutil.disk_usage(parent).free:
            return self.send({"id": rid, "ok": False, "error": "not enough free space on the laptop"})
        tmp = os.path.join(parent, f".{name}.{secrets.token_hex(6)}.nspart")
        self.upload = {"id": rid, "f": open(tmp, "xb"), "tmp": tmp, "final": final, "size": size, "got": 0}
        log.info("%s upload /%s (%d bytes)", self.email, os.path.relpath(final, self.root), size)
        self.send({"id": rid, "ok": True})

    def on_chunk(self, data):
        u = self.upload
        if not u:
            return
        u["got"] += len(data)
        if u["got"] > u["size"]:
            rid = u["id"]
            self.abort_upload()
            return self.send({"id": rid, "ok": False, "error": "upload larger than announced"})
        u["f"].write(data)

    def op_upload_end(self, rid):
        u = self.upload
        if not u:
            return self.send({"id": rid, "ok": False, "error": "no upload in progress"})
        self.upload = None
        u["f"].flush()
        os.fsync(u["f"].fileno())
        u["f"].close()
        if u["got"] != u["size"]:
            os.unlink(u["tmp"])
            return self.send({"id": rid, "ok": False, "error": "upload incomplete"})
        try:
            os.link(u["tmp"], u["final"])  # fails instead of overwriting if the name appeared meanwhile
            os.unlink(u["tmp"])
        except FileExistsError:
            os.unlink(u["tmp"])
            return self.send({"id": rid, "ok": False, "error": "a file with that name already exists"})
        except OSError:  # filesystem without hard links
            if os.path.lexists(u["final"]):
                os.unlink(u["tmp"])
                return self.send({"id": rid, "ok": False, "error": "a file with that name already exists"})
            os.rename(u["tmp"], u["final"])
        try:
            self.agent.set_record(u["final"], self.email, os.stat(u["final"]))
        except OSError:
            log.warning("%s: could not record upload of %s", self.email, u["final"])
        self.send({"id": rid, "ok": True})

    def abort_upload(self):
        u, self.upload = self.upload, None
        if u:
            try:
                u["f"].close()
                os.unlink(u["tmp"])
            except OSError:
                pass

    def op_mkdir(self, rid, req):
        if not self.cfg.get("allow_upload", False):
            return self.send({"id": rid, "ok": False, "error": "uploads are disabled"})
        name = req.get("name")
        if not valid_name(name):
            return self.send({"id": rid, "ok": False, "error": "bad folder name"})
        parent = resolve(self.root, req.get("path", "/"))
        new = os.path.join(parent, name)
        os.mkdir(new)
        self.agent.set_record(new, self.email, os.stat(new))
        self.send({"id": rid, "ok": True})


class Agent:
    def __init__(self, cfg, secret: bytes):
        self.cfg = cfg
        self.secret = secret
        self.root = os.path.realpath(cfg["root"])
        if not os.path.isdir(self.root) or self.root == "/":
            raise SystemExit("config 'root' must be an existing folder other than /")
        self.allowed = {e.lower() for e in cfg["allowed_emails"]}
        self.sessions = set()
        self.seen = {}  # sid -> expiry, blocks ticket replay
        self.tasks = set()
        self.meta_path = os.path.join(self.root, META_NAME)
        self.meta = self._load_meta()

    # --- who uploaded what, and when (hidden JSON file inside the root) ----------------------
    def _load_meta(self):
        try:
            with open(self.meta_path) as f:
                items = json.load(f)["items"]
            return items if isinstance(items, dict) else {}
        except FileNotFoundError:
            return {}
        except (ValueError, KeyError, OSError):
            log.warning("could not read %s, starting with empty records", self.meta_path)
            return {}

    def _save_meta(self):
        tmp = self.meta_path + ".nspart"
        with open(tmp, "w") as f:
            json.dump({"v": 1, "items": self.meta}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.meta_path)

    def _key(self, path):
        return os.path.relpath(path, self.root).replace(os.sep, "/")

    def set_record(self, path, email, st):
        self.meta[self._key(path)] = {"by": email, "at": int(time.time()),
                                      "size": st.st_size, "mtime_ns": st.st_mtime_ns}
        try:
            self._save_meta()
        except OSError as e:
            log.warning("could not save records: %s", e)

    def drop_record(self, path):
        if self.meta.pop(self._key(path), None) is not None:
            try:
                self._save_meta()
            except OSError as e:
                log.warning("could not save records: %s", e)

    def record_for(self, path, st, is_dir):
        """The upload record for a path, only if the file still looks like the one that was uploaded."""
        rec = self.meta.get(self._key(path))
        if not isinstance(rec, dict) or not isinstance(rec.get("by"), str) or not isinstance(rec.get("at"), int):
            return None
        if not is_dir and (rec.get("size") != st.st_size or rec.get("mtime_ns") != st.st_mtime_ns):
            return None
        return rec

    async def run(self):
        backoff = 1
        while True:
            try:
                async with connect(self.cfg["signaling_url"], max_size=1 << 20,
                                   ping_interval=20, ping_timeout=20) as ws:
                    await ws.send(json.dumps({"type": "agent_hello", "secret": self.secret.decode()}))
                    log.info("connected to signaling server")
                    backoff = 1
                    async for raw in ws:
                        t = asyncio.create_task(self.on_signal(ws, raw))
                        self.tasks.add(t)
                        t.add_done_callback(self.tasks.discard)
            except Exception as e:
                log.warning("signaling connection lost: %r", e)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def on_signal(self, ws, raw):
        try:
            msg = json.loads(raw)
            if msg.get("type") != "offer":
                return
            sid = msg["sid"]
            try:
                email = verify_ticket(self.secret, msg["ticket"], msg["sdp"], self.allowed)
            except TicketError as e:
                log.warning("rejected offer: %s", e)
                return await ws.send(json.dumps({"type": "reject", "sid": sid, "error": "not authorised"}))
            now = time.time()
            self.seen = {k: v for k, v in self.seen.items() if v > now}
            if sid in self.seen:
                return await ws.send(json.dumps({"type": "reject", "sid": sid, "error": "replayed"}))
            self.seen[sid] = now + 600
            if len(self.sessions) >= self.cfg.get("max_sessions", 3):
                return await ws.send(json.dumps({"type": "reject", "sid": sid, "error": "laptop is busy"}))
            answer = await self.start_session(email, msg["sdp"])
            await ws.send(json.dumps({"type": "answer", "sid": sid, "sdp": answer}))
        except Exception:
            log.exception("failed to handle offer")

    async def start_session(self, email, offer_sdp):
        ice = [RTCIceServer(urls=u) for u in self.cfg.get("ice_servers", ["stun:stun.l.google.com:19302"])]
        pc = RTCPeerConnection(RTCConfiguration(iceServers=ice))
        state = {"session": None}

        @pc.on("datachannel")
        def on_channel(channel):
            if state["session"] or channel.label != "files":
                channel.close()
                return
            s = Session(self, email, channel)
            state["session"] = s
            self.sessions.add(s)
            channel.on("message", s.on_message)
            channel.on("close", s.close)
            log.info("%s connected (%d active)", email, len(self.sessions))

            async def lifetime():
                task = asyncio.create_task(s.run())
                try:
                    await asyncio.wait_for(task, self.cfg.get("session_max_seconds", 43200))
                except asyncio.TimeoutError:
                    log.info("%s: session time limit reached", email)
                finally:
                    s.close()
                    self.sessions.discard(s)
                    await pc.close()
                    log.info("%s disconnected", email)
            asyncio.create_task(lifetime())

        @pc.on("connectionstatechange")
        async def on_state():
            if pc.connectionState in ("failed", "closed"):
                if state["session"]:
                    state["session"].close()
                await pc.close()

        async def watchdog():
            await asyncio.sleep(45)
            if not state["session"]:
                await pc.close()
        asyncio.create_task(watchdog())

        await pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type="offer"))
        await pc.setLocalDescription(await pc.createAnswer())
        return pc.localDescription.sdp


def main():
    p = argparse.ArgumentParser()
    p.add_argument("config")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    with open(args.config) as f:
        cfg = json.load(f)
    secret = os.environ["NAMESTORAGE_SECRET"].encode()
    if len(secret) < 32:
        raise SystemExit("NAMESTORAGE_SECRET must be at least 32 characters")
    asyncio.run(Agent(cfg, secret).run())


if __name__ == "__main__":
    main()
