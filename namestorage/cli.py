"""namestorage command line: setup wizard and configuration.

Every setting lives in ONE file, settings.json. The agent's config, the two secret
files and the systemd units are generated from it, so nobody edits them by hand.

Both programs run as systemd services, so they keep running after the SSH session
closes and come back after a reboot.

Environment overrides (used by the tests and for dry runs):
  NAMESTORAGE_CONFIG_DIR    default /etc/namestorage
  NAMESTORAGE_SYSTEMD_DIR   default /etc/systemd/system
  NAMESTORAGE_INSTALL_DIR   default /opt/namestorage
  NAMESTORAGE_NO_SYSTEMD=1  write files only, do not touch systemd
"""
import argparse
import copy
import importlib.util
import json
import os
import re
import pwd
import secrets
import shutil
import socket
import subprocess
import sys
import time

SERVICE_USER = "nsagent"
SERVICES = ("namestorage-signaling", "namestorage-agent")
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


def unit_dir():
    return os.environ.get("NAMESTORAGE_SYSTEMD_DIR", "/etc/systemd/system")


def install_dir():
    return os.environ.get("NAMESTORAGE_INSTALL_DIR", "/opt/namestorage")


def use_systemd():
    return os.environ.get("NAMESTORAGE_NO_SYSTEMD") != "1" and shutil.which("systemctl") is not None


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


def unit_text(name, s):
    """A systemd unit generated from the template in deploy/, adapted to these settings."""
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "deploy", name + ".service")
    with open(src) as f:
        text = f.read()
    text = text.replace("/opt/namestorage", install_dir()).replace("/etc/namestorage", cfg_dir())
    if name == "namestorage-agent":
        # the agent may only write inside the storage folder it was told to serve
        text = re.sub(r"(?m)^ReadWritePaths=.*$", "ReadWritePaths=" + s["root"], text)
        text = text.replace("After=network-online.target", "After=network-online.target namestorage-signaling.service")
        if s["root"].startswith(("/home/", "/root/")):
            text = text.replace("ProtectHome=true", "ProtectHome=false")
    return text


def ctl(*a):
    return subprocess.run(["systemctl", *a], capture_output=True, text=True)


def journal_tail(name, n=8):
    return subprocess.run(["journalctl", "-u", name, "-n", str(n), "--no-pager"],
                          capture_output=True, text=True).stdout


def prepare_root(root):
    """Create the storage folder and hand it to the service user. Returns a warning or None."""
    os.makedirs(root, exist_ok=True)
    if os.geteuid() != 0:
        return None
    try:
        pw = pwd.getpwnam(SERVICE_USER)
    except KeyError:
        return None
    os.chown(root, pw.pw_uid, pw.pw_gid)
    with os.scandir(root) as it:
        for i, e in enumerate(it):
            if i > 2000:
                break
            if e.stat(follow_symlinks=False).st_uid != pw.pw_uid:
                return (f"Some items in {root} are not owned by {SERVICE_USER}, so the drive may not "
                        f"be able to change them. Fix with: sudo chown -R {SERVICE_USER}:{SERVICE_USER} {root}")
    return None


def start_services(s):
    """Install the units, enable them at boot, (re)start them and check they came up."""
    for name in SERVICES:
        write_file(os.path.join(unit_dir(), name + ".service"), unit_text(name, s), 0o644)
    ctl("daemon-reload")
    for name in SERVICES:
        ctl("enable", name)
        ctl("restart", name)
    time.sleep(2)
    failed = False
    for name in SERVICES:
        state = ctl("is-active", name).stdout.strip()
        if state == "active":
            print(f"  ok  {name} is running")
        else:
            failed = True
            print(f"  x   {name} is {state or 'not running'}. Last log lines:")
            print("\n".join("        " + line for line in journal_tail(name).splitlines()))
    if failed:
        raise CliError("a service did not start (see above). A common cause is the port being used by "
                       "a program started by hand: stop it with Ctrl+C in its terminal, then run: "
                       "namestorage restart")


def apply(s):
    """Validate, write every generated file, and (re)start the services."""
    gaps = missing(s)
    if gaps:
        raise CliError("missing: " + ", ".join(gaps))
    warning = prepare_root(s["root"])
    write_all(s)
    print(f"Settings saved to {path('settings.json')}.")
    if warning:
        print("Note: " + warning)
    if use_systemd():
        print("Starting the services:")
        start_services(s)


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


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def tailscale_dns(ts):
    """This machine's public Tailscale name, e.g. laptop.tail1234.ts.net, or None."""
    r = run([ts, "status", "--json"])
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)["Self"]["DNSName"].rstrip(".") or None
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def setup_funnel(port, interactive=True):
    """Install and log in to Tailscale if needed, publish the port with Funnel in the background
    (so it survives the terminal) and return the wss:// address, or None if it did not work."""
    ts = shutil.which("tailscale")
    if not ts:
        if not interactive or not ask_yn("Tailscale is not installed. Install it now "
                                         "(runs the official installer from tailscale.com)?", True):
            return None
        r = subprocess.run("curl -fsSL https://tailscale.com/install.sh | sh", shell=True)
        ts = shutil.which("tailscale")
        if r.returncode != 0 or not ts:
            print("  x Could not install Tailscale.")
            return None
    if run([ts, "status"]).returncode != 0:
        print("Log in to Tailscale: open the link it prints, sign in, then come back here.")
        if subprocess.run([ts, "up"]).returncode != 0:
            print("  x Tailscale login did not finish.")
            return None
    for attempt in range(3):
        # Output is shown, not captured: if Funnel is off on your tailnet, Tailscale prints a
        # link to enable it and waits for you.
        if subprocess.run([ts, "funnel", "--bg", str(port)]).returncode == 0:
            break
        if not interactive or attempt == 2 or not ask_yn("Funnel did not start. Fix what it says above, then retry?", True):
            print("  x Could not start Funnel.")
            return None
    dns = tailscale_dns(ts)
    if not dns:
        print("  x Funnel is running, but this machine's Tailscale address could not be read.")
        return None
    print(f"  ok  Funnel publishes port {port} at https://{dns}")
    return "wss://" + dns


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
    elif args.funnel:
        url = setup_funnel(s["port"], interactive)
        if url:
            s["signaling_url"] = url
    elif interactive:
        keep = bool(s["signaling_url"]) and ask_yn(f"Keep the current public address ({s['signaling_url']})?", True)
        if not keep:
            url = None
            if args.funnel is not False and ask_yn(
                    "Publish the signaling server for free with Tailscale Funnel?", True):
                url = setup_funnel(s["port"])
            s["signaling_url"] = url or ask(
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
    if use_systemd():
        print("\nBoth programs now run in the background. You can close this terminal; they keep "
              "running and start again after a reboot.")
    print("\n" + next_steps(s))
    return 0


def require_settings():
    s = load()
    if s is None:
        raise CliError("not set up yet. Run: namestorage setup")
    return s


def next_steps(s):
    origins = "\n".join(f"       {o}" for o in s["allowed_origins"])
    return (
        "Next steps (the website itself is on GitHub, so these two are done there):\n"
        "  1. In docs/config.js in your repository, set these two lines, then commit and push:\n"
        f'       googleClientId: "{s["google_client_id"]}",\n'
        f'       signalingUrl: "{s["signaling_url"]}",\n'
        "  2. In Google Cloud Console, open your Web application client and make sure these are listed\n"
        "     under Authorized JavaScript origins:\n"
        f"{origins}\n"
        "  3. Open your site and sign in with an allowed account.\n"
        "Check everything at any time with: namestorage doctor")


def cmd_show(args):
    print(summary(require_settings()))
    return 0


def cmd_site_config(args):
    print(next_steps(require_settings()))
    return 0


# ------------------------------------------------------------- change settings
def int_range(lo, hi):
    def conv(v):
        try:
            n = int(str(v).strip())
        except ValueError:
            raise ValueError("expected a whole number")
        if not lo <= n <= hi:
            raise ValueError(f"expected a number from {lo} to {hi}")
        return n
    return conv


SCALARS = {
    "client-id": ("google_client_id", norm_client_id),
    "signaling-url": ("signaling_url", norm_ws_url),
    "root": ("root", norm_root),
    "allow-upload": ("allow_upload", norm_bool),
    "allow-delete": ("allow_delete", norm_bool),
    "min-free-bytes": ("min_free_bytes", int_range(0, 1 << 50)),
    "max-sessions": ("max_sessions", int_range(1, 50)),
    "session-max-seconds": ("session_max_seconds", int_range(60, 7 * 86400)),
    "port": ("port", int_range(1024, 65535)),
}


def modify(change):
    """Apply change(settings) to a copy, validate, write all files and restart. Nothing is
    written if the change is rejected."""
    new = copy.deepcopy(require_settings())
    try:
        change(new)
    except ValueError as e:
        raise CliError(str(e))
    apply(new)
    return new


def cmd_set(args):
    if args.key not in SCALARS:
        raise CliError(f"unknown setting '{args.key}'. Choose one of: {', '.join(SCALARS)}")
    field, norm = SCALARS[args.key]
    try:
        value = norm(args.value)
    except ValueError as e:
        raise CliError(f"{args.key}: {e}")
    old = require_settings()
    modify(lambda s: s.__setitem__(field, value))
    print(f"{args.key}: {old[field]} -> {value}")
    if args.key == "port":
        print("The Funnel still points at the old port. Run: namestorage setup")
    if args.key == "root":
        print(f"Existing files stay in {old['root']}; they are not moved.")
    return 0


def list_command(field, norm, noun):
    def run_it(args):
        s = require_settings()
        if args.action == "list":
            print("\n".join(s[field]) or f"(no {noun}s)")
            return 0
        if not args.values:
            raise CliError(f"give at least one {noun}")
        try:
            values = [norm(v) for v in args.values]
        except ValueError as e:
            raise CliError(str(e))

        def change(new):
            for v in values:
                if args.action == "add":
                    if v not in new[field]:
                        new[field].append(v)
                else:
                    if v not in new[field]:
                        raise ValueError(f"{v} is not in the list")
                    new[field].remove(v)
            if not new[field]:
                raise ValueError(f"cannot remove the last {noun}: nobody could sign in or connect")
        modify(change)
        print(f"{noun}s now: {', '.join(load()[field])}")
        return 0
    return run_it


def cmd_apply(args):
    apply(require_settings())
    return 0


def cmd_rotate_secret(args):
    modify(lambda s: s.__setitem__("secret", secrets.token_hex(32)))
    print("A new secret was generated and both programs restarted. The old one no longer works anywhere.")
    return 0


# ------------------------------------------------------------ service control
def service_states():
    return {n: (ctl("is-active", n).stdout.strip() or "unknown", ctl("is-enabled", n).stdout.strip() or "unknown")
            for n in SERVICES}


def port_listening(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def cmd_status(args):
    s = require_settings()
    if use_systemd():
        for name, (active, enabled) in service_states().items():
            print(f"  {name:24} {active:10} (starts at boot: {enabled})")
    else:
        print("  systemd is not available here")
    print(f"  signaling port {s['port']:<10} {'listening' if port_listening(s['port']) else 'not listening'}")
    ts = shutil.which("tailscale")
    if ts:
        out = run([ts, "funnel", "status"]).stdout.strip()
        print("  tailscale funnel:\n" + "\n".join("    " + line for line in (out or "not published").splitlines()))
    print("\n" + summary(s))
    return 0


def cmd_control(verb):
    def run_it(args):
        names = SERVICES if verb != "stop" else tuple(reversed(SERVICES))
        for name in names:
            ctl(verb, name)
        for name, (active, _) in service_states().items():
            print(f"  {name:24} {active}")
        return 0
    return run_it


def exec_cmd(cmd):
    os.execvp(cmd[0], cmd)


def cmd_logs(args):
    units = SERVICES if args.which == "all" else ("namestorage-" + args.which,)
    cmd = ["journalctl", "--no-pager", "-n", str(args.lines)]
    for u in units:
        cmd += ["-u", u]
    if args.follow:
        cmd.append("-f")
    exec_cmd(cmd)


def cmd_update(args):
    d = install_dir()
    if not os.path.isdir(os.path.join(d, ".git")):
        raise CliError(f"{d} is not a git checkout, so it cannot update itself. "
                       "Clone the repository again and run ./install.sh")
    if subprocess.run(["git", "-C", d, "pull", "--ff-only"]).returncode != 0:
        raise CliError("git pull failed. If the repository history was rewritten, run: "
                       f"sudo git -C {d} fetch origin && sudo git -C {d} reset --hard origin/main")
    subprocess.run([os.path.join(d, "venv", "bin", "pip"), "install", "-q",
                    "-r", os.path.join(d, "requirements-agent.txt"),
                    "-r", os.path.join(d, "requirements-signaling.txt")], check=False)
    # re-render with the NEW code, not the copy of this program that is already running
    env = dict(os.environ, PYTHONPATH=d)
    if subprocess.run([sys.executable, "-m", "namestorage.cli", "apply"], env=env).returncode != 0:
        raise CliError("updated the code, but restarting failed. See the messages above.")
    print("Updated and restarted.")
    return 0


# -------------------------------------------------------------------- doctor
def doctor_checks(s):
    """(ok, what was checked, what to do if it failed)"""
    checks = []
    gaps = missing(s)
    checks.append((not gaps, "all required settings are present", "missing: " + ", ".join(gaps) + ". Run: namestorage setup"))
    checks.append((len(s["secret"]) >= 32, "the shared secret is long enough", "Run: namestorage rotate-secret"))
    checks.append((os.path.isdir(s["root"]), f"storage folder {s['root']} exists", "Run: namestorage apply"))
    try:
        uid = pwd.getpwnam(SERVICE_USER).pw_uid
        owned = os.path.isdir(s["root"]) and os.stat(s["root"]).st_uid == uid
        checks.append((owned, f"storage folder is owned by {SERVICE_USER}",
                       f"Run: sudo chown -R {SERVICE_USER}:{SERVICE_USER} {s['root']}"))
    except KeyError:
        checks.append((False, f"the {SERVICE_USER} user exists", "Run ./install.sh"))
    for mod in ("aiortc", "websockets", "google.auth"):
        try:
            found = importlib.util.find_spec(mod) is not None
        except (ImportError, ValueError):
            found = False
        checks.append((found, f"python package {mod} is installed", "Run: namestorage update"))
    drift = [n for n, (content, _) in render_files(s).items()
             if not os.path.exists(path(n)) or open(path(n)).read() != content]
    checks.append((not drift, "generated files match the settings",
                   f"{', '.join(drift)} differ (edited by hand?). Run: namestorage apply"))
    if use_systemd():
        for name, (active, enabled) in service_states().items():
            checks.append((active == "active", f"{name} is running", f"See: namestorage logs {name.split('-')[1]}"))
            checks.append((enabled == "enabled", f"{name} starts at boot", "Run: namestorage apply"))
    checks.append((port_listening(s["port"]), f"the signaling server listens on port {s['port']}",
                   "See: namestorage logs signaling (is something else using the port?)"))
    ts = shutil.which("tailscale")
    if ts and s["signaling_url"].endswith(".ts.net"):
        out = run([ts, "funnel", "status"]).stdout
        checks.append((f"127.0.0.1:{s['port']}" in out, "Tailscale Funnel publishes the signaling port",
                       "Run: namestorage setup (and pick Funnel)"))
    return checks


def cmd_doctor(args):
    checks = doctor_checks(require_settings())
    for ok, what, fix in checks:
        print(f"  {'ok  ' if ok else 'FAIL'} {what}" + ("" if ok else f"\n       -> {fix}"))
    bad = sum(1 for ok, *_ in checks if not ok)
    print("\nEverything looks fine." if not bad else f"\n{bad} problem(s) found.")
    return 1 if bad else 0


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
    s.add_argument("--funnel", action=argparse.BooleanOptionalAction, default=None,
                   help="publish the signaling server with Tailscale Funnel (--no-funnel to skip the question)")
    s.add_argument("--root", type=argtype(norm_root))
    s.add_argument("--allow-upload", type=argtype(norm_bool), metavar="on|off")
    s.add_argument("--allow-delete", type=argtype(norm_bool), metavar="on|off")
    s.set_defaults(func=cmd_setup)

    sub.add_parser("show", help="print the current settings (secret hidden)").set_defaults(func=cmd_show)
    sub.add_parser("status", help="are the services running, and what are the settings").set_defaults(func=cmd_status)
    for verb in ("start", "stop", "restart"):
        sub.add_parser(verb, help=f"{verb} both services").set_defaults(func=cmd_control(verb))
    lg = sub.add_parser("logs", help="show the logs")
    lg.add_argument("which", nargs="?", choices=["all", "agent", "signaling"], default="all")
    lg.add_argument("-n", "--lines", type=int, default=50)
    lg.add_argument("-f", "--follow", action="store_true", help="keep following (Ctrl+C to stop)")
    lg.set_defaults(func=cmd_logs)

    st = sub.add_parser("set", help="change one setting: namestorage set allow-delete on")
    st.add_argument("key", metavar="setting", help="one of: " + ", ".join(SCALARS))
    st.add_argument("value")
    st.set_defaults(func=cmd_set)

    for name, field, norm, noun in (("email", "allowed_emails", norm_email, "email"),
                                    ("origin", "allowed_origins", norm_origin, "origin")):
        sp = sub.add_parser(name, help=f"add, remove or list allowed {noun}s")
        sp.add_argument("action", choices=["add", "remove", "list"])
        sp.add_argument("values", nargs="*")
        sp.set_defaults(func=list_command(field, norm, noun))

    sub.add_parser("rotate-secret", help="generate a new shared secret and restart").set_defaults(func=cmd_rotate_secret)
    sub.add_parser("apply", help="regenerate all files from the settings and restart").set_defaults(func=cmd_apply)
    sub.add_parser("update", help="pull the latest code, reinstall dependencies and restart").set_defaults(func=cmd_update)
    sub.add_parser("doctor", help="check that everything is set up correctly").set_defaults(func=cmd_doctor)
    sub.add_parser("site-config", help="what to put in docs/config.js and Google Console").set_defaults(func=cmd_site_config)
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
