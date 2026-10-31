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
    answers = iter([CID, "me@example.com", "https://me.github.io", str(root), "", "", "wss://box.ts.net", "n"])
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
