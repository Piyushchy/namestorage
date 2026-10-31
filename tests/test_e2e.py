"""End-to-end: simulated browser <-> signaling <-> agent over real WebRTC on localhost."""
import asyncio
import json
import os

import pytest
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from namestorage import signaling
from namestorage.agent import Agent

SECRET = b"s" * 40
ME = "me@example.com"


class Browser:
    def __init__(self):
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.dc = self.pc.createDataChannel("files")
        self.chunks, self.q, self.nid = [], asyncio.Queue(), 1
        self.dc.on("message", self._msg)

    def _msg(self, m):
        if isinstance(m, bytes):
            self.chunks.append(m)
        else:
            self.q.put_nowait(json.loads(m))

    async def open(self, url, token, origin="https://files.test"):
        await self.pc.setLocalDescription(await self.pc.createOffer())
        async with connect(url, origin=origin) as ws:
            await ws.send(json.dumps({"type": "offer", "token": token, "sdp": self.pc.localDescription.sdp}))
            reply = json.loads(await asyncio.wait_for(ws.recv(), 30))
        if reply["type"] != "answer":
            return reply
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=reply["sdp"], type="answer"))
        for _ in range(300):
            if self.dc.readyState == "open":
                return reply
            await asyncio.sleep(0.1)
        raise TimeoutError("data channel never opened")

    async def call(self, op, **kw):
        rid, self.nid = self.nid, self.nid + 1
        self.dc.send(json.dumps({"id": rid, "op": op, **kw}))
        while True:
            m = await asyncio.wait_for(self.q.get(), 30)
            if m.get("id") == rid:
                return m


@pytest.fixture
async def stack(tmp_path, monkeypatch):
    root = tmp_path / "share"
    root.mkdir()
    (root / "hello.txt").write_text("hello world")
    (tmp_path / "outside.txt").write_text("secret")
    monkeypatch.setattr(signaling, "verify_google",
                        lambda token, cid: token if token.endswith("@example.com") else (_ for _ in ()).throw(ValueError()))
    hub = signaling.Hub(SECRET, "cid", [ME], ["https://files.test"])
    server = await serve(hub.handler, "127.0.0.1", 0, max_size=65536)
    port = server.sockets[0].getsockname()[1]
    url = f"ws://127.0.0.1:{port}"
    cfg = {"root": str(root), "allowed_emails": [ME], "signaling_url": url, "allow_upload": True, "allow_delete": True,
           "min_free_bytes": 0, "ice_servers": []}
    agent = Agent(cfg, SECRET)
    hub.test_agent = agent
    task = asyncio.create_task(agent.run())
    for _ in range(100):
        if hub.agent:
            break
        await asyncio.sleep(0.05)
    yield url, root, tmp_path, hub
    task.cancel()
    server.close()


async def test_full_flow(stack):
    url, root, tmp, hub = stack
    b = Browser()
    assert (await b.open(url, ME))["type"] == "answer"

    r = await b.call("list", path="/")
    assert r["ok"] and [e["name"] for e in r["entries"]] == ["hello.txt"]

    # download (with the data coming back as binary chunks)
    r = await b.call("download", path="/hello.txt")
    assert r["ok"] and r["size"] == 11
    while True:
        m = await asyncio.wait_for(b.q.get(), 10)
        if m.get("done"):
            break
    assert b"".join(b.chunks) == b"hello world"

    # path escapes are refused
    for bad in ["../outside.txt", "/../outside.txt"]:
        assert not (await b.call("download", path=bad))["ok"]

    # mkdir + a multi-megabyte upload through many chunks
    assert (await b.call("mkdir", path="/", name="docs"))["ok"]
    payload = os.urandom(3 * 1024 * 1024 + 123)
    assert (await b.call("upload_start", path="/docs", name="big.bin", size=len(payload)))["ok"]
    for i in range(0, len(payload), 16384):
        b.dc.send(payload[i:i + 16384])
    assert (await b.call("upload_end"))["ok"]
    assert (root / "docs" / "big.bin").read_bytes() == payload
    assert not list(root.rglob("*.nspart"))

    # no overwrite, wrong size, bad names
    assert not (await b.call("upload_start", path="/docs", name="big.bin", size=1))["ok"]
    assert not (await b.call("upload_start", path="/", name="../evil", size=1))["ok"]
    assert (await b.call("upload_start", path="/", name="short.bin", size=10))["ok"]
    b.dc.send(b"abc")
    assert not (await b.call("upload_end"))["ok"]
    assert not (root / "short.bin").exists() and not list(root.rglob("*.nspart"))
    await b.pc.close()


async def test_wrong_login_refused(stack):
    url, *_ = stack
    for token in ["stranger@example.com", "not-a-valid-token"]:
        b = Browser()
        reply = await b.open(url, token)
        assert reply["type"] == "error"
        await b.pc.close()


async def test_wrong_origin_refused(stack):
    url, *_ = stack
    b = Browser()
    reply = await b.open(url, ME, origin="https://evil.test")
    assert reply["type"] == "error"
    await b.pc.close()


async def test_agent_rejects_forged_ticket(stack):
    """Even if someone reaches the agent through signaling, a ticket they cannot sign is refused."""
    url, root, tmp, hub = stack
    b = Browser()
    await b.pc.setLocalDescription(await b.pc.createOffer())
    sdp = b.pc.localDescription.sdp
    from namestorage.ticket import make_ticket
    bad = make_ticket(b"w" * 40, ME, sdp, "forged-sid")  # wrong secret
    fut = asyncio.get_running_loop().create_future()
    hub.pending["forged-sid"] = fut
    await hub.agent.send(json.dumps({"type": "offer", "sid": "forged-sid", "sdp": sdp, "ticket": bad}))
    reply = await asyncio.wait_for(fut, 10)
    assert reply["type"] == "reject"
    await b.pc.close()


async def test_records_and_delete(stack):
    url, root, tmp, hub = stack
    b = Browser()
    assert (await b.open(url, ME))["type"] == "answer"

    # a file that was not uploaded through the site has no record
    r = await b.call("list", path="/")
    assert r["caps"] == {"upload": True, "delete": True} and r["email"] == ME
    assert "by" not in r["entries"][0]

    # upload and mkdir leave a record: who and when
    assert (await b.call("mkdir", path="/", name="docs"))["ok"]
    assert (await b.call("upload_start", path="/docs", name="a.txt", size=3))["ok"]
    b.dc.send(b"abc")
    assert (await b.call("upload_end"))["ok"]
    r = await b.call("list", path="/docs")
    e = r["entries"][0]
    assert e["name"] == "a.txt" and e["by"] == ME and abs(e["at"] - __import__("time").time()) < 30
    top = {x["name"]: x for x in (await b.call("list", path="/"))["entries"]}
    assert top["docs"]["by"] == ME and "by" not in top["hello.txt"]

    # the record file exists but is hidden and untouchable
    assert (root / ".namestorage-meta.json").exists()
    assert ".namestorage-meta.json" not in top
    for op, kw in [("download", {"path": "/.namestorage-meta.json"}),
                   ("delete", {"path": "/", "name": ".namestorage-meta.json"}),
                   ("upload_start", {"path": "/", "name": ".namestorage-meta.json", "size": 1}),
                   ("mkdir", {"path": "/", "name": ".namestorage-meta.json"})]:
        assert not (await b.call(op, **kw))["ok"], op

    # a file replaced outside the site loses its (now wrong) record
    (root / "docs" / "a.txt").write_text("changed on disk")
    assert "by" not in (await b.call("list", path="/docs"))["entries"][0]

    # delete: non-empty folder refused, then file, then folder; records go away
    assert not (await b.call("delete", path="/", name="docs"))["ok"]
    assert (await b.call("delete", path="/docs", name="a.txt"))["ok"]
    assert not (root / "docs" / "a.txt").exists()
    assert (await b.call("delete", path="/", name="docs"))["ok"]
    assert not (root / "docs").exists()
    import json as _j
    assert _j.loads((root / ".namestorage-meta.json").read_text())["items"] == {}
    assert not (await b.call("delete", path="/", name="nothing"))["ok"]

    # escapes and symlinks are refused
    assert not (await b.call("delete", path="/", name="../outside.txt"))["ok"]
    assert not (await b.call("delete", path="/../", name="outside.txt"))["ok"]
    os.symlink(tmp / "outside.txt", root / "link")
    assert not (await b.call("delete", path="/", name="link"))["ok"]
    assert (tmp / "outside.txt").exists()
    await b.pc.close()


async def test_delete_disabled_by_default(stack):
    url, root, tmp, hub = stack
    hub.test_agent.cfg["allow_delete"] = False  # the fixture enables it; check the off state
    b = Browser()
    assert (await b.open(url, ME))["type"] == "answer"
    assert (await b.call("list", path="/"))["caps"]["delete"] is False
    assert not (await b.call("delete", path="/", name="hello.txt"))["ok"]
    assert (root / "hello.txt").exists()
    await b.pc.close()
