import os
import pytest
from namestorage.paths import PathError, resolve, valid_name
from namestorage.ticket import TicketError, make_ticket, verify_ticket

FP = ":".join(["AB"] * 32)
SDP = f"v=0\r\na=fingerprint:sha-256 {FP}\r\nm=application 9\r\n"
SECRET = b"x" * 40
ALLOWED = {"me@example.com"}


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "root"
    r.mkdir()
    (r / "a.txt").write_text("hi")
    (tmp_path / "secret.txt").write_text("nope")
    os.symlink(tmp_path / "secret.txt", r / "link.txt")
    os.symlink(tmp_path, r / "linkdir")
    return os.path.realpath(r)


def test_paths_ok(root):
    assert resolve(root, "/a.txt") == os.path.join(root, "a.txt")
    assert resolve(root, "/") == root
    assert resolve(root, "") == root


@pytest.mark.parametrize("bad", ["../secret.txt", "/../secret.txt", "a/../../x", "link.txt",
                                 "linkdir/secret.txt", "a\x00b", "..\\x", "/etc/passwd/../../.."])
def test_paths_blocked(root, bad):
    try:
        out = resolve(root, bad)
    except PathError:
        return
    # absolute-looking input is treated as relative to root, which is safe
    assert os.path.commonpath([root, out]) == root


def test_symlink_escape_blocked(root):
    with pytest.raises(PathError):
        resolve(root, "link.txt")
    with pytest.raises(PathError):
        resolve(root, "linkdir/secret.txt")


@pytest.mark.parametrize("name,ok", [("a.txt", True), ("", False), ("..", False), ("a/b", False),
                                     ("x.nspart", False), ("a" * 300, False), ("é.pdf", True)])
def test_names(name, ok):
    assert valid_name(name) is ok


def test_ticket_roundtrip():
    t = make_ticket(SECRET, "Me@Example.com", SDP, "sid1", now=1000)
    assert verify_ticket(SECRET, t, SDP, ALLOWED, now=1001) == "me@example.com"


def test_ticket_rejections():
    t = make_ticket(SECRET, "me@example.com", SDP, "sid1", now=1000)
    with pytest.raises(TicketError):  # expired
        verify_ticket(SECRET, t, SDP, ALLOWED, now=2000)
    with pytest.raises(TicketError):  # wrong secret
        verify_ticket(b"y" * 40, t, SDP, ALLOWED, now=1001)
    with pytest.raises(TicketError):  # different browser (other fingerprint)
        other = SDP.replace("AB", "CD")
        verify_ticket(SECRET, t, other, ALLOWED, now=1001)
    with pytest.raises(TicketError):  # email not on the agent's own allow-list
        verify_ticket(SECRET, t, SDP, {"other@example.com"}, now=1001)
    forged = dict(t, email="evil@example.com")
    with pytest.raises(TicketError):
        verify_ticket(SECRET, forged, SDP, {"evil@example.com"}, now=1001)
    with pytest.raises(TicketError):
        verify_ticket(SECRET, {"junk": 1}, SDP, ALLOWED, now=1001)
