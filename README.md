# namestorage

Your own web drive: open a website, sign in with Google, and upload or download files that live on **your own laptop**. Files travel **directly between the browser and the laptop** (WebRTC, always encrypted). No file passes through a server, so there is no upload or download size limit from any middleman, and users install nothing.

```
Browser ──(1) Google login + connection offer──► signaling server ──► laptop agent
Browser ◄──────────(2) direct encrypted WebRTC data channel (files)──────────► laptop agent
```

| Part | Where it runs | What it does |
|---|---|---|
| `docs/` | GitHub Pages at your domain | Login page and file manager UI |
| `namestorage/signaling.py` | Any small always-on host with HTTPS/WSS | Verifies the Google login, introduces browser and laptop. Carries a few KB per session, never file data. |
| `namestorage/agent.py` | Your Ubuntu laptop | Serves one folder. Only makes outbound connections (works behind CGNAT). |

## Security model

- Google sign-in tokens are verified **server-side** (signature, audience, expiry, verified email) against an **email allow-list**.
- The signaling server issues a short-lived **ticket** bound to the browser's DTLS fingerprint. The **agent re-checks the ticket and its own allow-list** itself, so a stolen ticket cannot be used from another browser, and replays are rejected.
- The agent serves **one folder only**: `..` is rejected, symlinks are never followed, and it runs as a dedicated low-privilege user (see the systemd unit).
- Uploads never overwrite existing files, are written to a temp file and renamed only when complete, and are refused if disk space is short.
- Origin check, rate limit and message size limits on the signaling server.

Known limits, stated plainly:

1. **The signaling server is trusted.** It holds the shared secret. If someone takes it over, they can introduce themselves to your laptop. Keep it patched and keep the secret out of git.
2. **Peer-to-peer can fail.** Some networks (strict CGNAT, corporate firewalls) block direct connections. Then you need a TURN relay (for example coturn on a small VPS) added to `ice_servers` in both `docs/config.js` and the agent config. A relay carries data, so it brings back a middleman and its bandwidth.
3. **Your limits remain:** home upload speed, your ISP data plan, and the laptop being on and awake.
4. Not independently audited. Don't use it for confidential or employer data.

## Tests

```bash
pip install -r requirements-agent.txt -r requirements-signaling.txt pytest pytest-asyncio
pytest
```

The suite covers path escapes (including symlinks), ticket forgery/expiry/mismatch, and a real WebRTC session on localhost: login, listing, download, a 3 MB chunked upload, refused overwrite, refused bad names, wrong login and wrong origin. What is **not** tested here: real Google tokens, real browsers, and NAT traversal over the internet.

## Setup

### 1. Google sign-in client
In Google Cloud Console create an OAuth client of type **Web application** and add these **Authorized JavaScript origins**: `https://piyushchy.github.io` and `http://localhost:8000` (for testing). Put the client ID in `docs/config.js`. (Menu names change; follow Google's current "Sign in with Google for Web" guide.)

### 2. Signaling server
Needs any always-on host with a public HTTPS/WSS address (a small VPS, or a free app host; compare current free-tier terms yourself).

```bash
python3 -m venv venv && . venv/bin/activate
pip install -r requirements-signaling.txt
cp deploy/signaling.env.example signaling.env      # edit it; secret: openssl rand -hex 32
set -a; . ./signaling.env; set +a
python -m namestorage.signaling
```
Put a TLS reverse proxy (Caddy/nginx) in front so the browser can use `wss://`. Set `signalingUrl` in `docs/config.js`.

### 3. Agent on the laptop
```bash
sudo useradd --system --home /opt/namestorage nsagent
sudo mkdir -p /opt/namestorage /etc/namestorage /srv/storage/web
sudo cp -r . /opt/namestorage && cd /opt/namestorage
sudo python3 -m venv venv && sudo venv/bin/pip install -r requirements-agent.txt
sudo cp deploy/agent.example.json /etc/namestorage/agent.json    # edit: emails, signaling_url
echo "NAMESTORAGE_SECRET=<same secret as the signaling server>" | sudo tee /etc/namestorage/agent.env
sudo chmod 600 /etc/namestorage/agent.env
sudo chown -R nsagent:nsagent /srv/storage/web
sudo cp deploy/namestorage-agent.service /etc/systemd/system/
sudo systemctl enable --now namestorage-agent
journalctl -u namestorage-agent -f
```
`root` should be a **dedicated folder**, not your whole disk.

### 4. The website
Repo Settings > Pages > deploy from branch `main`, folder `/docs`. The site is then free at `https://piyushchy.github.io/namestorage/`. (A custom domain is optional: add a `docs/CNAME` file and a DNS CNAME record, and update the allowed origins.)

## Not included yet
Delete/rename, resumable transfers, per-user folders, share links, a TURN relay.
