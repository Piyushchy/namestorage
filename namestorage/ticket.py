"""Connection tickets.

The signaling server verifies the Google login, then mints a short-lived ticket
bound to (session id, email, DTLS fingerprint of the browser's offer). The agent
re-verifies the ticket itself, so a ticket captured by anyone else is useless:
they cannot produce a connection with the same DTLS fingerprint.
"""
import hashlib
import hmac
import json
import re
import time

_FP = re.compile(r"^a=fingerprint:sha-256 ([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){31})\s*$", re.M)


class TicketError(Exception):
    pass


def extract_fingerprint(sdp: str) -> str:
    fps = {m.upper() for m in _FP.findall(sdp)}
    if len(fps) != 1:
        raise ValueError("SDP must contain exactly one sha-256 fingerprint")
    return fps.pop()


def _mac(secret: bytes, sid: str, email: str, exp: int, fp: str) -> str:
    msg = json.dumps(["v1", sid, email, exp, fp], separators=(",", ":")).encode()
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def make_ticket(secret: bytes, email: str, sdp: str, sid: str, ttl: int = 120, now=None) -> dict:
    fp = extract_fingerprint(sdp)
    email = email.lower()
    exp = int((now if now is not None else time.time()) + ttl)
    return {"sid": sid, "email": email, "exp": exp, "fp": fp,
            "mac": _mac(secret, sid, email, exp, fp)}


def verify_ticket(secret: bytes, ticket, sdp: str, allowed_emails, now=None, max_ttl: int = 300) -> str:
    """Return the authorised email, or raise TicketError."""
    try:
        sid, email, fp, mac = ticket["sid"], ticket["email"], ticket["fp"], ticket["mac"]
        exp = int(ticket["exp"])
    except (KeyError, TypeError, ValueError):
        raise TicketError("malformed ticket")
    if not all(isinstance(x, str) for x in (sid, email, fp, mac)):
        raise TicketError("malformed ticket")
    now = now if now is not None else time.time()
    if exp < now:
        raise TicketError("expired")
    if exp > now + max_ttl:
        raise TicketError("lifetime too long")
    if not hmac.compare_digest(_mac(secret, sid, email, exp, fp), mac):
        raise TicketError("bad signature")
    try:
        if extract_fingerprint(sdp) != fp.upper():
            raise TicketError("ticket does not match this connection")
    except ValueError:
        raise TicketError("bad offer")
    if email.lower() not in allowed_emails:
        raise TicketError("email not allowed")
    return email.lower()
