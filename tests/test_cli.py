"""Tests for the setup wizard and settings (no root, no systemd needed)."""
import json
import os
import stat

import pytest

from namestorage import cli

CID = "123456789-abcdef.apps.googleusercontent.com"


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = tmp_path / "etc"
    monkeypatch.setenv("NAMESTORAGE_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("NAMESTORAGE_NO_SYSTEMD", "1")
    monkeypatch.setenv("NAMESTORAGE_SYSTEMD_DIR", str(tmp_path / "units"))
    monkeypatch.setenv("NAMESTORAGE_INSTALL_DIR", "/opt/ns-test")
    return cfg, tmp_path / "storage"


def setup_args(root, *extra):
    return ["setup", "--yes", "--client-id", CID, "--email", "Me@Example.com",
            "--origin", "https://me.github.io/", "--signaling-url", "https://box.ts.net/",
            "--root", str(root), *extra]


def settings(cfg):
    return json.loads((cfg / "settings.json").read_text())


# ---------------------------------------------------------------- validators
@pytest.mark.parametrize("fn,good,expected", [
    (cli.norm_email, " Me@Example.COM ", "me@example.com"),
    (cli.norm_origin, "https://me.github.io/", "https://me.github.io"),
    (cli.norm_origin, "http://localhost:8000", "http://localhost:8000"),
    (cli.norm_client_id, CID, CID),
    (cli.norm_ws_url, "https://box.ts.net/", "wss://box.ts.net"),
    (cli.norm_ws_url, "wss://box.ts.net:8443", "wss://box.ts.net:8443"),
    (cli.norm_root, "/srv/storage/web/", "/srv/storage/web"),
    (cli.norm_bool, "On", True),
    (cli.norm_bool, "no", False),
])
def test_valid_values(fn, good, expected):
    assert fn(good) == expected


@pytest.mark.parametrize("fn,bad", [
    (cli.norm_email, "not-an-email"),
    (cli.norm_email, "a@b"),
    (cli.norm_origin, "https://me.github.io/namestorage"),   # paths are not origins
    (cli.norm_origin, "http://evil.com"),                     # plain http only for localhost
    (cli.norm_origin, "me.github.io"),
    (cli.norm_client_id, "abc"),
    (cli.norm_ws_url, "ws://evil.com"),
    (cli.norm_ws_url, "ftp://x"),
    (cli.norm_root, "relative/path"),
    (cli.norm_root, "/"),
    (cli.norm_root, "/home"),
    (cli.norm_bool, "maybe"),
])
def test_invalid_values(fn, bad):
    with pytest.raises(ValueError):
        fn(bad)


# --------------------------------------------------------------- full setup
def test_setup_writes_everything_from_one_file(env):
    cfg, root = env
    assert cli.main(setup_args(root)) == 0
    s = settings(cfg)
    assert s["allowed_emails"] == ["me@example.com"] and s["allowed_origins"] == ["https://me.github.io"]
    assert s["signaling_url"] == "wss://box.ts.net" and len(s["secret"]) == 64
    assert root.is_dir()

    agent = json.loads((cfg / "agent.json").read_text())
    assert agent["root"] == str(root) and agent["allowed_emails"] == ["me@example.com"]
    assert agent["signaling_url"] == "ws://127.0.0.1:8765"     # same laptop, no dependency on the Funnel
    assert agent["allow_upload"] is True and agent["allow_delete"] is False

    sig = cli.parse_env(str(cfg / "signaling.env"))
    assert sig["GOOGLE_CLIENT_ID"] == CID and sig["ALLOWED_ORIGINS"] == "https://me.github.io"
    assert sig["NAMESTORAGE_SECRET"] == s["secret"] == cli.parse_env(str(cfg / "agent.env"))["NAMESTORAGE_SECRET"]
    assert sig["HOST"] == "127.0.0.1" and sig["TRUST_PROXY"] == "1"

    # secrets are not readable by other users
    for name in ("settings.json", "agent.env", "signaling.env"):
        assert stat.S_IMODE(os.stat(cfg / name).st_mode) == 0o600


def test_rerun_keeps_secret_and_previous_answers(env):
    cfg, root = env
    cli.main(setup_args(root))
    first = settings(cfg)
    assert cli.main(["setup", "--yes", "--allow-delete", "on"]) == 0
    second = settings(cfg)
    assert second["secret"] == first["secret"]
    assert second["allowed_emails"] == first["allowed_emails"] and second["allow_delete"] is True
    assert json.loads((cfg / "agent.json").read_text())["allow_delete"] is True


def test_missing_values_are_reported_and_nothing_is_written(env, capsys):
    cfg, root = env
    assert cli.main(["setup", "--yes", "--email", "me@example.com"]) == 1
    assert "missing" in capsys.readouterr().err
    assert not (cfg / "settings.json").exists()


def test_bad_option_value_is_rejected(env):
    cfg, root = env
    with pytest.raises(SystemExit):
        cli.main(["setup", "--yes", "--origin", "https://x.io/path"])
    assert not cfg.exists() or not (cfg / "settings.json").exists()


def test_interactive_wizard_asks_validates_and_confirms(env, monkeypatch, capsys):
    cfg, root = env
    answers = iter([
        "bogus-id", CID,                      # client id: first rejected, then accepted
        "me@example.com, you@example.com",    # emails
        "https://me.github.io/path",          # origin rejected (has a path)
        "https://me.github.io",               # origin accepted
        str(root),                            # folder
        "",                                   # uploads: default yes
        "y",                                  # deleting
        "n",                                  # no Tailscale Funnel
        "wss://box.ts.net",                   # signaling address
        "",                                   # confirm: default yes
    ])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert cli.main(["setup"]) == 0
    out = capsys.readouterr().out
    assert "does not look like a Google client ID" in out and "not a valid origin" in out
    s = settings(cfg)
    assert s["allowed_emails"] == ["me@example.com", "you@example.com"]
    assert s["allow_delete"] is True and s["allow_upload"] is True


def test_declining_the_confirmation_changes_nothing(env, monkeypatch):
    cfg, root = env
    answers = iter([CID, "me@example.com", "https://me.github.io", str(root), "", "", "n", "wss://box.ts.net", "n"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert cli.main(["setup"]) == 1
    assert not (cfg / "settings.json").exists()


def test_existing_manual_install_is_imported_as_defaults(env):
    cfg, root = env
    cfg.mkdir()
    secret = "ab" * 32
    (cfg / "signaling.env").write_text(
        f"GOOGLE_CLIENT_ID={CID}\nALLOWED_EMAILS=old@example.com\n"
        f"ALLOWED_ORIGINS=https://old.github.io\nNAMESTORAGE_SECRET={secret}\nPORT=9000\n")
    (cfg / "agent.json").write_text(json.dumps(
        {"signaling_url": "wss://old.ts.net/", "root": str(root), "allowed_emails": ["old@example.com"],
         "allow_upload": True, "allow_delete": True}))
    assert cli.main(["setup", "--yes"]) == 0
    s = settings(cfg)
    assert s["secret"] == secret and s["port"] == 9000 and s["allow_delete"] is True
    assert s["signaling_url"] == "wss://old.ts.net" and s["allowed_origins"] == ["https://old.github.io"]
    # the generated agent file now points at the local signaling port
    assert json.loads((cfg / "agent.json").read_text())["signaling_url"] == "ws://127.0.0.1:9000"


def test_show_hides_the_secret(env, capsys):
    cfg, root = env
    cli.main(setup_args(root))
    capsys.readouterr()
    assert cli.main(["show"]) == 0
    out = capsys.readouterr().out
    assert settings(cfg)["secret"] not in out and "hidden" in out


# ----------------------------------------------------------------- services
class FakeCtl:
    def __init__(self, states=None):
        self.calls, self.states = [], states or {}

    def __call__(self, *a):
        self.calls.append(a)
        out = self.states.get(a[1], "active\n") if a[0] == "is-active" else ""
        return type("R", (), {"stdout": out, "returncode": 0})()


def test_units_follow_the_settings(env):
    cfg, root = env
    cli.main(setup_args(root))
    s = settings(cfg)
    agent = cli.unit_text("namestorage-agent", s)
    assert f"ReadWritePaths={root}" in agent and "/srv/storage/web" not in agent
    assert "ExecStart=/opt/ns-test/venv/bin/python -m namestorage.agent " + str(cfg / "agent.json") in agent
    assert f"EnvironmentFile={cfg}/agent.env" in agent and "namestorage-signaling.service" in agent
    assert "ProtectHome=true" in agent
    sig = cli.unit_text("namestorage-signaling", s)
    assert f"EnvironmentFile={cfg}/signaling.env" in sig and "-m namestorage.signaling" in sig
    # a storage folder under /home needs ProtectHome off, or the agent could not see it
    s["root"] = "/home/spark/drive"
    assert "ProtectHome=false" in cli.unit_text("namestorage-agent", s)


def test_setup_installs_enables_and_starts_both_services(env, monkeypatch, capsys):
    cfg, root = env
    monkeypatch.delenv("NAMESTORAGE_NO_SYSTEMD")
    monkeypatch.setattr(cli.shutil, "which", lambda n: "/bin/systemctl")
    fake = FakeCtl()
    monkeypatch.setattr(cli, "ctl", fake)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    assert cli.main(setup_args(root)) == 0
    units = cfg.parent / "units"
    assert (units / "namestorage-agent.service").exists() and (units / "namestorage-signaling.service").exists()
    verbs = [c[0] for c in fake.calls]
    assert verbs[0] == "daemon-reload"
    assert ("enable", "namestorage-signaling") in fake.calls and ("enable", "namestorage-agent") in fake.calls
    assert ("restart", "namestorage-signaling") in fake.calls and ("restart", "namestorage-agent") in fake.calls
    assert "close this terminal" in capsys.readouterr().out


def test_a_service_that_fails_is_reported_with_its_log(env, monkeypatch, capsys):
    cfg, root = env
    monkeypatch.delenv("NAMESTORAGE_NO_SYSTEMD")
    monkeypatch.setattr(cli.shutil, "which", lambda n: "/bin/systemctl")
    monkeypatch.setattr(cli, "ctl", FakeCtl({"namestorage-signaling": "failed\n"}))
    monkeypatch.setattr(cli, "journal_tail", lambda n, k=8: "OSError: [Errno 98] address already in use")
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    assert cli.main(setup_args(root)) == 1
    cap = capsys.readouterr()
    assert "namestorage-signaling is failed" in cap.out and "address already in use" in cap.out
    assert "did not start" in cap.err


# ------------------------------------------------------------------ funnel
class FakeTailscale:
    """Stands in for subprocess.run so no real tailscale or network is used."""
    def __init__(self, logged_in=True, funnel_results=(0,), dns="laptop.tail1234.ts.net."):
        self.logged_in, self.funnel_results, self.dns, self.calls = logged_in, list(funnel_results), dns, []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        out, rc = "", 0
        if isinstance(cmd, str):                       # the installer one-liner
            rc = 0
        elif cmd[1] == "status" and "--json" in cmd:
            out = json.dumps({"Self": {"DNSName": self.dns}})
        elif cmd[1] == "status":
            rc = 0 if self.logged_in else 1
        elif cmd[1] == "up":
            self.logged_in = True
        elif cmd[1] == "funnel":
            rc = self.funnel_results.pop(0) if self.funnel_results else 0
        return type("R", (), {"returncode": rc, "stdout": out, "stderr": ""})()


def run_wizard(env, monkeypatch, answers, tailscale=None, installed=True):
    cfg, root = env
    answers = iter([CID, "me@example.com", "https://me.github.io", str(root), "", "", *answers])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    if tailscale:
        monkeypatch.setattr(cli.subprocess, "run", tailscale)
    monkeypatch.setattr(cli.shutil, "which", lambda n: "/usr/bin/tailscale" if installed and n == "tailscale" else None)
    return cli.main(["setup"])


def test_funnel_gives_the_public_address(env, monkeypatch):
    ts = FakeTailscale()
    assert run_wizard(env, monkeypatch, ["y", ""], ts) == 0        # use Funnel, confirm
    assert settings(env[0])["signaling_url"] == "wss://laptop.tail1234.ts.net"
    assert ["/usr/bin/tailscale", "funnel", "--bg", "8765"] in ts.calls     # background mode


def test_declining_funnel_asks_for_an_address(env, monkeypatch):
    ts = FakeTailscale()
    assert run_wizard(env, monkeypatch, ["n", "wss://my.example.com", ""], ts) == 0
    assert settings(env[0])["signaling_url"] == "wss://my.example.com"
    assert not any(isinstance(c, list) and c[1] == "funnel" for c in ts.calls)


def test_tailscale_not_installed_and_install_declined(env, monkeypatch):
    # y: use Funnel, n: do not install, then type the address by hand, confirm
    assert run_wizard(env, monkeypatch, ["y", "n", "wss://my.example.com", ""], FakeTailscale(), installed=False) == 0
    assert settings(env[0])["signaling_url"] == "wss://my.example.com"


def test_logs_in_when_needed_and_retries_a_failed_funnel(env, monkeypatch):
    ts = FakeTailscale(logged_in=False, funnel_results=(1, 0))
    assert run_wizard(env, monkeypatch, ["y", "y", ""], ts) == 0   # use Funnel, retry after failure, confirm
    assert ["/usr/bin/tailscale", "up"] in ts.calls
    assert sum(1 for c in ts.calls if isinstance(c, list) and c[1] == "funnel") == 2
    assert settings(env[0])["signaling_url"] == "wss://laptop.tail1234.ts.net"


def test_giving_up_on_funnel_falls_back_to_typing(env, monkeypatch):
    ts = FakeTailscale(funnel_results=(1,))
    assert run_wizard(env, monkeypatch, ["y", "n", "wss://my.example.com", ""], ts) == 0
    assert settings(env[0])["signaling_url"] == "wss://my.example.com"


def test_rerun_can_keep_the_current_address(env, monkeypatch):
    cfg, root = env
    cli.main(setup_args(root))
    answers = iter(["", "", "", "", "", "", "", ""])    # accept every default, keep address, confirm
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert cli.main(["setup"]) == 0
    assert settings(cfg)["signaling_url"] == "wss://box.ts.net"


def test_non_interactive_funnel_flag(env, monkeypatch):
    cfg, root = env
    monkeypatch.setattr(cli.subprocess, "run", FakeTailscale())
    monkeypatch.setattr(cli.shutil, "which", lambda n: "/usr/bin/tailscale")
    args = [a for a in setup_args(root) if a not in ("--signaling-url", "https://box.ts.net/")]
    assert cli.main(args + ["--funnel"]) == 0
    assert settings(cfg)["signaling_url"] == "wss://laptop.tail1234.ts.net"
