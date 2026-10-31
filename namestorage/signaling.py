"""Signaling server: checks the Google login and introduces browser <-> agent.

It carries only a few KB of connection setup per session and never sees file data.
Config (environment): GOOGLE_CLIENT_ID, ALLOWED_EMAILS (comma separated),
NAMESTORAGE_SECRET (>=32 chars, shared with the agent), ALLOWED_ORIGINS
(comma separated, e.g. https://piyushchy.github.io), HOST, PORT,
TRUST_PROXY=1 if behind a reverse proxy that sets X-Forwarded-For.
"""
import asyncio
import collections
import hmac
import json
import logging
import os
import time
import uuid

from websockets.asyncio.server import serve

from .ticket import extract_fingerprint, make_ticket

log = logging.getLogger("namestorage.signaling")


def verify_google(token: str, client_id: str) -> str:
    """Verify a Google ID token (signature, issuer, audience, expiry). Returns email."""
    from google.auth.transport import requests as greq
    from google.oauth2 import id_token
    info = id_token.verify_oauth2_token(token, greq.Request(), client_id)
    if not info.get("email_verified"):
        raise ValueError("email not verified")
    return info["email"].lower()


class Hub:
    def __init__(self, secret: bytes, client_id: str, allowed_emails, allowed_origins, trust_proxy=False):
        if len(secret) < 32:
            raise ValueError("NAMESTORAGE_SECRET must be at least 32 characters")
        self.secret = secret
        self.client_id = client_id
        self.allowed_emails = {e.lower() for e in allowed_emails}
        self.allowed_origins = set(allowed_origins)
        self.trust_proxy = trust_proxy
        self.agent = None
        self.pending = {}  # sid -> Future resolved with the agent's reply
        self.hits = collections.defaultdict(collections.deque)

    def client_ip(self, ws) -> str:
        if self.trust_proxy:
            xff = ws.request.headers.get("X-Forwarded-For", "")
            if xff:
                return xff.split(",")[-1].strip()
        return (ws.remote_address or ("?",))[0]

    def rate_ok(self, ip: str, limit=10, window=60) -> bool:
        q, now = self.hits[ip], time.time()
        while q and q[0] < now - window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True

    async def handler(self, ws):
        try:
            msg = json.loads(await asyncio.wait_for(ws.recv(), 10))
            kind = msg.get("type")
            if kind == "agent_hello":
                await self.agent_session(ws, msg)
            elif kind == "offer":
                await self.browser_session(ws, msg)
        except Exception as e:  # malformed input, timeouts, disconnects
            log.debug("connection ended: %r", e)
        finally:
            await ws.close()

    async def agent_session(self, ws, msg):
        given = msg.get("secret")
        if not isinstance(given, str) or not hmac.compare_digest(given.encode(), self.secret):
            log.warning("agent auth failed from %s", self.client_ip(ws))
            return
        if self.agent is not None:
            try:
                await self.agent.close()
            except Exception:
                pass
        self.agent = ws
        log.info("agent online")
        try:
            async for raw in ws:
                m = json.loads(raw)
                fut = self.pending.get(m.get("sid"))
                if fut and not fut.done() and m.get("type") in ("answer", "reject"):
                    fut.set_result(m)
        finally:
            if self.agent is ws:
                self.agent = None
                log.info("agent offline")

    async def browser_session(self, ws, msg):
        origin = ws.request.headers.get("Origin")
        if self.allowed_origins and origin not in self.allowed_origins:
            return await self.fail(ws, "origin not allowed")
        if not self.rate_ok(self.client_ip(ws)):
            return await self.fail(ws, "too many attempts, wait a minute")
        sdp, token = msg.get("sdp"), msg.get("token")
        if not isinstance(sdp, str) or not isinstance(token, str) or len(sdp) > 60000:
            return await self.fail(ws, "bad request")
        try:
            email = await asyncio.get_running_loop().run_in_executor(
                None, verify_google, token, self.client_id)
        except Exception:
            return await self.fail(ws, "login invalid or expired, sign in again")
        if email not in self.allowed_emails:
            log.warning("login refused for %s", email)
            return await self.fail(ws, "this account is not allowed")
        if self.agent is None:
            return await self.fail(ws, "the storage laptop is offline")
        try:
            extract_fingerprint(sdp)
        except ValueError:
            return await self.fail(ws, "bad offer")
        sid = uuid.uuid4().hex
        ticket = make_ticket(self.secret, email, sdp, sid)
        fut = asyncio.get_running_loop().create_future()
        self.pending[sid] = fut
        try:
            await self.agent.send(json.dumps({"type": "offer", "sid": sid, "sdp": sdp, "ticket": ticket}))
            reply = await asyncio.wait_for(fut, 30)
        except asyncio.TimeoutError:
            return await self.fail(ws, "the laptop did not answer")
        finally:
            self.pending.pop(sid, None)
        if reply.get("type") == "answer" and isinstance(reply.get("sdp"), str):
            log.info("introduced %s", email)
            await ws.send(json.dumps({"type": "answer", "sdp": reply["sdp"]}))
        else:
            await self.fail(ws, str(reply.get("error", "refused"))[:100])

    async def fail(self, ws, error):
        await ws.send(json.dumps({"type": "error", "error": error}))


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    env = os.environ
    hub = Hub(
        secret=env["NAMESTORAGE_SECRET"].encode(),
        client_id=env["GOOGLE_CLIENT_ID"],
        allowed_emails=[e.strip() for e in env["ALLOWED_EMAILS"].split(",") if e.strip()],
        allowed_origins=[o.strip() for o in env.get("ALLOWED_ORIGINS", "").split(",") if o.strip()],
        trust_proxy=env.get("TRUST_PROXY") == "1",
    )
    if not hub.allowed_origins:
        log.warning("ALLOWED_ORIGINS is empty: any website may use this signaling server")
    async with serve(hub.handler, env.get("HOST", "127.0.0.1"), int(env.get("PORT", "8765")), max_size=65536):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
