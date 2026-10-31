"""namestorage command line: setup wizard and configuration.

Every setting lives in ONE file, settings.json. The agent's config, the two secret
files and the systemd units are generated from it, so nobody edits them by hand.

Environment overrides (used by the tests and for dry runs):
  NAMESTORAGE_CONFIG_DIR   default /etc/namestorage
"""
import argparse
import json
import os
import re
import secrets
import shutil
import sys

SERVICE_USER = "nsagent"
DEFAULT_ROOT = "/srv/storage/web"

DEFAULTS = {
    "google_client_id": "",
    "allowed_emails": [],
    "allowed_origins": [],
    "signaling_url": "",          # public wss:// address the browser uses
    "root": DEFAULT_ROOT,
    "allow_upload": True,
    "allow_delete": False,
    "min_free_bytes": 1 << 30,
    "max_sessions": 3,
    "session_max_seconds": 43200,
    "port": 8765,                 # local port of the signaling server
    "secret": "",
}


class CliError(Exception):
    pass


def cfg_dir():
    return os.environ.get("NAMESTORAGE_CONFIG_DIR", "/etc/namestorage")


def path(name):
    return os.path.join(cfg_dir(), name)


# ---------------------------------------------------------------- validation
EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
CLIENT_RE = re.compile(r"^[0-9]+-[A-Za-z0-9_]+\.apps\.googleusercontent\.com$")
ORIGIN_RE = re.compile(r"^(https://[A-Za-z0-9.-]+(:\d+)?|http://(localhost|127\.0\.0\.1)(:\d+)?)$")
WSS_RE = re.compile(r"^(wss://[A-Za-z0-9.-]+(:\d+)?(/\S*)?|ws://(localhost|127\.0\.0\.1)(:\d+)?(/\S*)?)$")
SYSTEM_DIRS = {"/", "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/boot", "/proc", "/sys",
               "/dev", "/var", "/opt", "/root", "/home", "/tmp", "/run", "/srv"}


def norm_email(v):
    v = v.strip().lower()
    if not EMAIL_RE.match(v):
        raise ValueError(f"'{v}' is not an email address")
    return v


def norm_origin(v):
    v = v.strip().rstrip("/")
    if not ORIGIN_RE.match(v):
        raise ValueError(f"'{v}' is not a valid origin. Use the site address with no path, "
                         "like https://yourname.github.io")
    return v


def norm_client_id(v):
    v = v.strip()
    if not CLIENT_RE.match(v):
        raise ValueError("that does not look like a Google client ID "
                         "(it ends with .apps.googleusercontent.com)")
    return v


def norm_ws_url(v):
    v = v.strip()
    if v.startswith("https://"):
        v = "wss://" + v[len("https://"):]
    v = v.rstrip("/")
    if not WSS_RE.match(v):
        raise ValueError("the signaling address must start with wss:// (for example "
                         "wss://yourlaptop.tailnet.ts.net)")
    return v


def norm_root(v):
    v = os.path.normpath(v.strip())
    if not os.path.isabs(v):
        raise ValueError("the storage folder must be an absolute path like /srv/storage/web")
    if v in SYSTEM_DIRS:
        raise ValueError(f"{v} is a system folder; choose a dedicated folder such as {DEFAULT_ROOT}")
    return v


def norm_bool(v):
    if isinstance(v, bool):
        return v
    t = str(v).strip().lower()
    if t in ("1", "true", "yes", "y", "on"):
        return True
    if t in ("0", "false", "no", "n", "off"):
        return False
    raise ValueError("answer yes or no")


def split_list(raw, norm):
    out = []
    for part in re.split(r"[,\s;]+", raw.strip()):
        if part:
            item = norm(part)
            if item not in out:
                out.append(item)
    return out


# ------------------------------------------------------------------ settings
def load():
    try:
        with open(path("settings.json")) as f:
            raw = json.load(f)
    except FileNotFoundError:
        return None
    except ValueError as e:
        raise CliError(f"{path('settings.json')} is not valid JSON ({e})")
    s = dict(DEFAULTS)
    s.update(raw)
    return s


def write_file(p, content, mode, group=None):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        f.write(content)
    os.chmod(tmp, mode)
    if group and os.geteuid() == 0:
        try:
            shutil.chown(tmp, user="root", group=group)
        except (LookupError, OSError):
            pass
    os.replace(tmp, p)


def save(s):
    write_file(path("settings.json"), json.dumps(s, indent=2) + "\n", 0o600)


def parse_env(p):
    out = {}
    try:
        with open(p) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    return out


def import_legacy():
    """Defaults taken from a manual install (signaling.env, agent.env, agent.json), if present."""
    sig, agent_env = parse_env(path("signaling.env")), parse_env(path("agent.env"))
    try:
        with open(path("agent.json")) as f:
            agent = json.load(f)
    except (OSError, ValueError):
        agent = {}
    if not (sig or agent or agent_env):
        return None
    s = dict(DEFAULTS)

    def take(key, value, norm):
        try:
            s[key] = norm(value)
        except (ValueError, TypeError):
            pass

    take("google_client_id", sig.get("GOOGLE_CLIENT_ID", ""), norm_client_id)
    take("allowed_emails", sig.get("ALLOWED_EMAILS") or ",".join(agent.get("allowed_emails", [])),
         lambda v: split_list(v, norm_email))
    take("allowed_origins", sig.get("ALLOWED_ORIGINS", ""), lambda v: split_list(v, norm_origin))
    if str(agent.get("signaling_url", "")).startswith("wss://"):
        take("signaling_url", agent["signaling_url"], norm_ws_url)
    if agent.get("root"):
        take("root", agent["root"], norm_root)
    for k in ("allow_upload", "allow_delete"):
        if k in agent:
            take(k, agent[k], norm_bool)
    for k in ("min_free_bytes", "max_sessions", "session_max_seconds"):
        if isinstance(agent.get(k), int):
            s[k] = agent[k]
    if sig.get("PORT", "").isdigit():
        s["port"] = int(sig["PORT"])
    secret = sig.get("NAMESTORAGE_SECRET") or agent_env.get("NAMESTORAGE_SECRET", "")
    if len(secret) >= 32:
        s["secret"] = secret
    return s


def missing(s):
    m = []
    if not s["google_client_id"]:
        m.append("Google client ID")
    if not s["allowed_emails"]:
        m.append("allowed email")
    if not s["allowed_origins"]:
        m.append("allowed origin")
    if not s["signaling_url"]:
        m.append("public signaling address")
    if len(s["secret"]) < 32:
        m.append("secret")
    return m


# ----------------------------------------------------------------- rendering
def render_files(s):
    """The generated files: name -> (content, mode)."""
    agent = {
        # The signaling server runs on this same laptop, so the agent reaches it locally
        # and does not depend on the public address being up.
        "signaling_url": f"ws://127.0.0.1:{s['port']}",
        "root": s["root"],
        "allowed_emails": s["allowed_emails"],
        "allow_upload": s["allow_upload"],
        "allow_delete": s["allow_delete"],
        "min_free_bytes": s["min_free_bytes"],
        "max_sessions": s["max_sessions"],
        "session_max_seconds": s["session_max_seconds"],
        "ice_servers": ["stun:stun.l.google.com:19302"],
    }
    signaling_env = "\n".join([
        f"GOOGLE_CLIENT_ID={s['google_client_id']}",
        f"ALLOWED_EMAILS={','.join(s['allowed_emails'])}",
        f"ALLOWED_ORIGINS={','.join(s['allowed_origins'])}",
        f"NAMESTORAGE_SECRET={s['secret']}",
        "HOST=127.0.0.1",
        f"PORT={s['port']}",
        "TRUST_PROXY=1",
    ]) + "\n"
    return {
        "agent.json": (json.dumps(agent, indent=2) + "\n", 0o640),
        "agent.env": (f"NAMESTORAGE_SECRET={s['secret']}\n", 0o600),
        "signaling.env": (signaling_env, 0o600),
    }


def write_all(s):
    save(s)
    for name, (content, mode) in render_files(s).items():
        write_file(path(name), content, mode, group=SERVICE_USER)


def apply(s):
    """Validate, write every generated file. (Services are handled by later steps.)"""
    gaps = missing(s)
    if gaps:
        raise CliError("missing: " + ", ".join(gaps))
    os.makedirs(s["root"], exist_ok=True)
    write_all(s)


# -------------------------------------------------------------------- wizard
def ask(label, default="", norm=lambda v: v, hint=""):
    if hint:
        print(f"  {hint}")
    while True:
        shown = f" [{default}]" if default else ""
        raw = input(f"{label}{shown}: ").strip() or default
        if not raw:
            print("  This is required.")
            continue
        try:
            return norm(raw)
        except ValueError as e:
            print(f"  x {e}")


def ask_yn(label, default):
    while True:
        raw = input(f"{label} [{'Y/n' if default else 'y/N'}]: ").strip()
        if not raw:
            return default
        try:
            return norm_bool(raw)
        except ValueError:
            print("  Please answer y or n.")


def mask(secret):
    return f"set ({len(secret)} characters, hidden)" if secret else "not set"


def summary(s):
    return "\n".join([
        f"  Google client ID : {s['google_client_id']}",
        f"  Allowed emails   : {', '.join(s['allowed_emails'])}",
        f"  Allowed origins  : {', '.join(s['allowed_origins'])}",
        f"  Public address   : {s['signaling_url']}",
        f"  Storage folder   : {s['root']}",
        f"  Uploads allowed  : {'yes' if s['allow_upload'] else 'no'}",
        f"  Deleting allowed : {'yes' if s['allow_delete'] else 'no'}",
        f"  Secret           : {mask(s['secret'])}",
    ])


def collect(args, s):
    """Fill in s from flags, then (unless --yes) from questions with current values as defaults."""
    interactive = not args.yes

    if args.client_id:
        s["google_client_id"] = args.client_id
    elif interactive:
        s["google_client_id"] = ask(
            "Google client ID", s["google_client_id"], norm_client_id,
            "From Google Cloud Console > Clients > your Web application client.")

    if args.email:
        s["allowed_emails"] = args.email
    elif interactive:
        s["allowed_emails"] = ask(
            "Allowed emails (comma separated)", ",".join(s["allowed_emails"]),
            lambda v: split_list(v, norm_email),
            "Only these Google accounts can sign in.")

    if args.origin:
        s["allowed_origins"] = args.origin
    elif interactive:
        s["allowed_origins"] = ask(
            "Allowed origins (comma separated)", ",".join(s["allowed_origins"]),
            lambda v: split_list(v, norm_origin),
            "The address your website loads from, no path. Example: https://yourname.github.io")

    if args.root:
        s["root"] = args.root
    elif interactive:
        s["root"] = ask("Storage folder", s["root"], norm_root,
                        "The one folder this drive serves. Use a dedicated folder.")

    if args.allow_upload is not None:
        s["allow_upload"] = args.allow_upload
    elif interactive:
        s["allow_upload"] = ask_yn("Allow uploads?", s["allow_upload"])

    if args.allow_delete is not None:
        s["allow_delete"] = args.allow_delete
    elif interactive:
        s["allow_delete"] = ask_yn("Allow deleting files and empty folders?", s["allow_delete"])

    if args.signaling_url:
        s["signaling_url"] = args.signaling_url
    elif interactive:
        s["signaling_url"] = ask(
            "Public signaling address (wss://...)", s["signaling_url"], norm_ws_url,
            "How browsers reach the signaling server on this laptop.")

    if len(s["secret"]) < 32:
        s["secret"] = secrets.token_hex(32)
    return s


def cmd_setup(args):
    s = load()
    if s is None:
        s = import_legacy()
        if s is not None:
            print("Found an existing manual install; its values are used as defaults.\n")
    if s is None:
        s = dict(DEFAULTS)
    if not args.yes:
        print("namestorage setup. Press Enter to keep the value shown in [brackets].\n")
    s = collect(args, s)
    gaps = missing(s)
    if gaps:
        raise CliError("missing: " + ", ".join(gaps) + " (pass them as options with --yes)")
    print("\nSettings:\n" + summary(s) + "\n")
    if not args.yes and not ask_yn("Apply these settings?", True):
        print("Nothing was changed.")
        return 1
    apply(s)
    print(f"Settings saved to {path('settings.json')}.")
    return 0


def cmd_show(args):
    s = load()
    if s is None:
        raise CliError("not set up yet. Run: namestorage setup")
    print(summary(s))
    return 0


# ---------------------------------------------------------------------- main
def ensure_root():
    """Re-run the same command through sudo when not root."""
    if os.geteuid() == 0 or os.environ.get("NAMESTORAGE_CONFIG_DIR"):
        return
    pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.execvp("sudo", ["sudo", "env", f"PYTHONPATH={pkg_root}", sys.executable,
                       "-m", "namestorage.cli", *sys.argv[1:]])


def argtype(norm):
    def convert(v):
        try:
            return norm(v)
        except ValueError as e:
            raise argparse.ArgumentTypeError(str(e))
    return convert


def build_parser():
    p = argparse.ArgumentParser(prog="namestorage", description="Set up and manage your namestorage drive.")
    sub = p.add_subparsers(dest="cmd", metavar="command")

    s = sub.add_parser("setup", help="ask for the settings and apply them")
    s.add_argument("--yes", action="store_true", help="do not ask questions; use options and saved values")
    s.add_argument("--client-id", type=argtype(norm_client_id))
    s.add_argument("--email", action="append", type=argtype(norm_email), help="repeat for several emails")
    s.add_argument("--origin", action="append", type=argtype(norm_origin), help="repeat for several origins")
    s.add_argument("--signaling-url", type=argtype(norm_ws_url))
    s.add_argument("--root", type=argtype(norm_root))
    s.add_argument("--allow-upload", type=argtype(norm_bool), metavar="on|off")
    s.add_argument("--allow-delete", type=argtype(norm_bool), metavar="on|off")
    s.set_defaults(func=cmd_setup)

    sub.add_parser("show", help="print the current settings (secret hidden)").set_defaults(func=cmd_show)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    if argv is None:
        ensure_root()
    try:
        return args.func(args) or 0
    except CliError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled. Nothing was changed.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
