# namestorage

A personal web drive. You open a website, sign in with Google, and upload or download files that live on **your own Ubuntu laptop**. Files travel **directly between your browser and the laptop** over an encrypted WebRTC data channel. No file ever passes through a server, so no middleman imposes a size limit, and nothing is installed on the device you browse from.

Live site (this setup): `https://piyushchy.is-a.dev/namestorage/` (GitHub Pages, from the `/docs` folder).

---

## Contents

1. [How it works](#how-it-works)
2. [Components](#components)
3. [Connection workflow, step by step](#connection-workflow-step-by-step)
4. [What happens during a download and an upload](#what-happens-during-a-download-and-an-upload)
5. [Security model](#security-model)
6. [Limitations](#limitations)
7. [Requirements](#requirements)
8. [Installation](#installation)
9. [Configuration reference](#configuration-reference)
10. [Running and maintaining it](#running-and-maintaining-it)
11. [Troubleshooting](#troubleshooting)
12. [Tests](#tests)
13. [Repository layout](#repository-layout)
14. [Not included yet](#not-included-yet)

---

## How it works

Three programs cooperate. Two of them only help the browser and the laptop find each other.

```
                     (1) Google sign-in
 Browser  ───────────────────────────────────►  Google
 (GitHub Pages site)  ◄─── signed ID token ───

 Browser ──(2) token + connection offer──► Signaling server ──(3) offer + ticket──► Laptop agent
 Browser ◄──(5) answer ────────────────── (checks login,      ◄──(4) answer─────── (checks ticket,
                                           mints ticket)                             allow-list)

 Browser ◄════════ (6) direct WebRTC data channel: file list, downloads, uploads ════════► Laptop agent
                        (encrypted, peer to peer; the signaling server is no longer involved)
```

- The **website** is static files on GitHub Pages. It holds no secrets and no data.
- The **signaling server** is a tiny always-on program. It verifies your Google login and passes connection setup messages (a few KB) between the browser and the laptop. It never sees file data.
- The **laptop agent** runs on your Ubuntu machine and serves exactly one folder. It only makes **outbound** connections (to the signaling server), so you do not open any port at home and it works behind CGNAT.

In this setup the signaling server also runs on the same laptop and is published to the internet with **Tailscale Funnel**, which gives it a free public `wss://` address without a card or a VPS.

---

## Components

| Part | Where it runs | What it does |
|---|---|---|
| `docs/` (`index.html`, `app.js`, `config.js`) | GitHub Pages | Login button, file manager UI, and the browser side of the protocol. |
| `namestorage/signaling.py` | Any always-on host; here, the laptop at `127.0.0.1:8765` | Verifies Google tokens, enforces the email allow-list, origin check and rate limit, mints tickets, relays offer and answer. |
| `namestorage/agent.py` | The laptop (systemd service `namestorage-agent`) | Verifies tickets, accepts WebRTC sessions, serves one folder (list, download, upload, make folder). |
| `namestorage/ticket.py` | Both servers | Creates and verifies HMAC-signed tickets bound to the browser's DTLS fingerprint. |
| `namestorage/paths.py` | Agent | Keeps every path inside the root folder; validates file names. |
| Tailscale Funnel | The laptop | Publishes `127.0.0.1:8765` at `https://<machine>.<tailnet>.ts.net/` (use `wss://` for the same address). |
| Google OAuth client | Google Cloud Console | Lets the site use "Sign in with Google". The client ID is public. |

---

## Connection workflow, step by step

1. **Page load.** The browser loads the site from GitHub Pages. `config.js` supplies the Google client ID, the signaling URL and the STUN servers.
2. **Sign in.** The user clicks the Google button. Google returns a signed **ID token** (a JWT) to the page. The page keeps it in memory only.
3. **Connect.** The user clicks Connect. The browser creates a WebRTC peer connection with one data channel called `files`, makes an SDP **offer**, and waits for ICE candidate gathering (up to 6 s).
4. **Offer to signaling.** The browser opens a WebSocket to `signalingUrl` and sends `{type: "offer", token, sdp}`.
5. **Server checks.** The signaling server, in order:
   - checks the WebSocket `Origin` header against `ALLOWED_ORIGINS`;
   - applies a rate limit (10 attempts per minute per IP);
   - checks the offer is small (60 KB max) and well formed;
   - verifies the Google token (signature, issuer, audience = your client ID, expiry, `email_verified`);
   - checks the email is in `ALLOWED_EMAILS`;
   - checks that the laptop agent is currently connected;
   - checks the offer has exactly one SHA-256 DTLS fingerprint.
6. **Ticket.** If all checks pass, the server mints a **ticket**: an HMAC (keyed with `NAMESTORAGE_SECRET`) over a session id, the email, an expiry (120 s) and the browser's DTLS fingerprint. It forwards `{offer, ticket}` to the agent.
7. **Agent checks.** The agent re-verifies the ticket itself: valid MAC, not expired, lifetime not suspiciously long, **fingerprint matches the offer**, email in its own allow-list. It also rejects a replayed session id and refuses when `max_sessions` are already active. Then it creates the WebRTC **answer**.
8. **Answer back.** The agent sends the answer to the signaling server, which relays it to the browser.
9. **Direct channel.** Browser and agent complete ICE (using STUN to find a path) and DTLS. The data channel opens. From here on the signaling server is not involved, and the page asks the agent for the root folder listing.
10. **Watchdog and limits.** If no data channel opens within 45 s the agent drops the attempt. Sessions end when the channel closes or after `session_max_seconds` (12 h by default).

Because the DTLS fingerprint is signed into the ticket, a stolen ticket is useless: a different browser cannot present the same fingerprint.

---

## What happens during a download and an upload

All messages use the single data channel: JSON text for commands and replies, binary for file chunks. Chunks are 16 KB; both sides pause sending while the channel buffer is above 1 MB so memory stays flat.

| Operation | Request | Behavior |
|---|---|---|
| `list` | `{op:"list", path}` | Returns files and folders (max 5000, folders first), each with the upload record if there is one (`by`, `at`), plus the signed-in email and what this laptop allows (`caps`). Symlinks, temporary upload files and the records file are hidden. |
| `download` | `{op:"download", path}` | Replies with the size, streams binary chunks, then `{done:true}`. A `cancel` message stops it. |
| `upload_start` | `{op:"upload_start", path, name, size}` | Refused if uploads are disabled, another upload is running, the name is invalid or already exists, or free space would drop below `min_free_bytes`. Creates a hidden temp file `.name.xxxx.nspart` in the target folder. |
| (chunks) | binary | Written to the temp file. If more bytes arrive than announced, the upload is aborted. |
| `upload_end` | `{op:"upload_end"}` | Flushes and fsyncs. If the byte count matches, the temp file is hard-linked to the final name (which fails rather than overwriting) and the temp file is removed. If the size is wrong, the temp file is deleted. |
| `mkdir` | `{op:"mkdir", path, name}` | Creates a folder (only when uploads are allowed). |
| `delete` | `{op:"delete", path, name}` | Deletes one file or one **empty** folder (only when `allow_delete` is on). Refuses symlinks, anything outside the root, and the records file. |

In the browser, Chrome and Edge stream downloads straight to disk through the File System Access API. Other browsers collect the whole file in memory first (see limitations).

### Upload records (who uploaded what, and when)

When a file is uploaded, or a folder is created, through the site, the agent writes the signed-in email and the time into a hidden JSON file, `.namestorage-meta.json`, in the root folder. The page shows these in the **Uploaded** and **By** columns.

- The records file is hidden from listings and cannot be downloaded, uploaded over, deleted or created through the site.
- Files that were put on the laptop some other way have no record. The page shows their last-modified time, dimmed, and "—" for the uploader.
- A record is stored with the file's size and modification time. If the file is changed or replaced on the laptop behind the site's back, the record no longer matches and is ignored, so the page never attributes a different file to the wrong person.
- Deleting a file through the site removes its record.
- The record is a convenience log written by the agent, not a tamper-proof audit trail: anyone with shell access to the laptop can edit it.

### Deleting

Deleting is **off by default**. Turn it on with `"allow_delete": true` in `agent.json`. The page then shows a trash icon on each row and asks for confirmation. Only files and empty folders can be deleted (a folder with content is refused, so one click can never wipe a tree). Deletion is permanent: there is no recycle bin.

### Share links

Each row shows a share-link symbol. It is a placeholder and does nothing yet; see "Not included yet".

---

## Security model

- **Google sign-in tokens are verified server-side**, not trusted from the page, against an **email allow-list**. The client ID itself is public and safe to commit; only your listed emails get in.
- **Two independent allow-lists and checks.** The signaling server and the agent each check the email. The agent trusts the ticket only if it can verify the MAC with the shared secret.
- **Tickets are bound to the browser's DTLS fingerprint**, expire in 2 minutes and cannot be replayed.
- **Deleting is opt-in and limited.** It needs `allow_delete`, works on one file or one empty folder per request, never follows symlinks, and cannot touch the records file or anything outside the root.
- **One folder only.** `..` is rejected, backslashes and NUL bytes are rejected, paths are resolved with `realpath` and must stay inside the root, symlinks are not listed. The root cannot be `/`. The agent should run as a dedicated low-privilege user (the systemd unit does this, with `ProtectSystem=strict`, `ProtectHome=true`, `NoNewPrivileges=true`, and write access only to the storage folder).
- **Uploads never overwrite**, are written to a temp file and renamed only when complete and the size matches, and are refused when disk space is short.
- **Signaling hardening.** Origin check, per-IP rate limit, 64 KB WebSocket message limit, 10 s to send the first message, 30 s for the agent to answer.
- **Encryption.** The WebRTC data channel is always DTLS-encrypted. The signaling connection is `wss://` (TLS provided by Tailscale Funnel).

What the secrets are:

| Value | Secret? | Where it lives |
|---|---|---|
| Google client ID | No, public | `docs/config.js`, `signaling.env` |
| `NAMESTORAGE_SECRET` | **Yes** | `signaling.env` and `/etc/namestorage/agent.env` only. Never in git, never pasted in chats or issues. If it leaks, generate a new one and update both files. |
| Google client secret | Not used by this flow | Ignore it if the console shows one |

---

## Limitations

Stated plainly:

1. **The signaling server is trusted.** It holds the shared secret. Whoever controls it can introduce a browser to your laptop (they still need a Google login on your allow-list for the normal path, but a takeover of the server plus the secret defeats that). Keep it patched and keep the secret out of git.
2. **Your laptop must be on, awake and online**, with the agent, the signaling server and the Funnel all running. If any is down the site shows "the storage laptop is offline" or cannot reach the server.
3. **Peer-to-peer can fail.** Only STUN is configured. Strict CGNAT, symmetric NAT, some corporate or college Wi-Fi, and some mobile carriers block direct connections, and the page shows "Could not open a direct connection". The fix is a TURN relay (for example coturn on a small VPS) added to `ice_servers` in both `docs/config.js` and the agent config, but a relay carries your data and brings back a middleman and its bandwidth.
4. **Speed is bounded by your home upload speed** (for downloads) and your ISP plan. Throughput is also capped by 16 KB chunking and WebRTC's single data channel, so expect well below raw line speed on fast links.
5. **Large downloads in Firefox and Safari** are buffered in browser memory before saving (they lack the File System Access API), so very large files can fail there. Use Chrome or Edge for big files.
6. **One transfer at a time per browser session**, one upload at a time per session, at most `max_sessions` (default 3) concurrent sessions, and sessions end after 12 hours.
7. **No resume.** An interrupted transfer starts over (an interrupted upload leaves nothing behind except the cleaned-up temp file).
8. **Google sign-in specifics.** The OAuth client's authorized JavaScript origins must contain the exact site origin. Because the portfolio repo uses the custom domain `piyushchy.is-a.dev`, GitHub serves this project at that domain, so the origin is `https://piyushchy.is-a.dev` (not `piyushchy.github.io`). If the consent screen is in "Testing" mode, only listed test users can sign in. The Google account you use must be the one in `ALLOWED_EMAILS`.
9. **The origin is shared with the portfolio.** Anything hosted on that origin can attempt the login flow. The allow-list still applies, but a separate subdomain would isolate it better.
10. **Funnel is public.** Anyone can reach the signaling URL. Bots will probe it (you will see `426 Upgrade Required` lines in the log; these are harmless plain-HTTP requests that the server rejects). Free-tier terms of Tailscale and Google can change; check current terms.
11. **No rename, move, share links or per-user folders** (see "Not included yet"). All allowed users see the same folder, and every allowed user can delete anything in it when deleting is enabled. Folders can only be deleted when empty.
12. **Upload records are partial.** Only files and folders created through the site have an uploader and time. Anything added to the folder directly on the laptop shows only its modified time.
13. **Not independently audited.** Do not use it for confidential or employer data.

---

## Requirements

- An Ubuntu (or similar Linux) machine that stays on, with Python 3.10+ (3.12 tested), `python3-venv`, `git`, `openssl`, `systemd`.
- A GitHub account (free) for the repo and Pages.
- A Google account and a Google Cloud project for the OAuth client ID. Creating the client ID is free, but Google controls its own sign-up flow; if it asks for a card, see Troubleshooting.
- A free Tailscale account for Funnel (or any other way to expose a `wss://` endpoint, for example a VPS with Caddy).

---

## Installation

Do these in order. Run the commands on the laptop unless stated.

### 1. Get the code and run the tests

```bash
sudo apt update && sudo apt install -y git python3 python3-venv openssl
git clone https://github.com/Piyushchy/namestorage.git
cd namestorage
python3 -m venv venv && . venv/bin/activate
pip install -r requirements-agent.txt -r requirements-signaling.txt pytest pytest-asyncio
pytest          # expect: 25 passed
```

### 2. Create the Google sign-in client (in the browser)

1. Open Google Cloud Console, then the **Clients** page, and create or choose a project.
2. Create a client of type **Web application**.
3. Under **Authorized JavaScript origins** add the exact origin of your site with no trailing slash and no path:
   - `https://piyushchy.is-a.dev` (the real one for this setup; also add `https://piyushchy.github.io` if you like)
   - `http://localhost:8000` (only if you want to test locally)
4. Save (changes can take a few minutes). Copy the **client ID**.
5. If the OAuth consent screen is in Testing mode, add your Gmail as a test user.

Put the client ID in `docs/config.js` as `googleClientId` and commit it (it is public).

### 3. Configure the signaling server

```bash
cd ~/namestorage
cp deploy/signaling.env.example signaling.env
openssl rand -hex 32                 # copy the output: this is your secret
nano signaling.env
```

Set (see the [reference](#configuration-reference)):

```
GOOGLE_CLIENT_ID=<your id>.apps.googleusercontent.com
ALLOWED_EMAILS=you@gmail.com
ALLOWED_ORIGINS=https://piyushchy.is-a.dev,https://piyushchy.github.io
NAMESTORAGE_SECRET=<the 64-character string>
HOST=127.0.0.1
PORT=8765
TRUST_PROXY=1
```

Quick manual start to check it works:

```bash
. venv/bin/activate
set -a; . ./signaling.env; set +a
python -m namestorage.signaling      # expect: server listening on 127.0.0.1:8765
```

### 4. Publish it with Tailscale Funnel

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up                    # open the printed link and sign in (free)
sudo tailscale funnel --bg 8765      # if it says Funnel is off, open the link it prints, enable it, run again
sudo tailscale funnel status         # shows https://<machine>.<tailnet>.ts.net/
```

Your signaling URL is that address with `wss://`, for example `wss://piyush.tail799736.ts.net`. (Without `--bg`, Funnel stops when the terminal closes.)

Set it in `docs/config.js` as `signalingUrl`, and commit and push.

### 5. Install the agent

```bash
cd ~/namestorage && git pull
sudo useradd --system --home /opt/namestorage nsagent        # skip if the user exists
sudo mkdir -p /opt/namestorage /etc/namestorage /srv/storage/web
sudo rm -rf /opt/namestorage/* 
sudo cp -r . /opt/namestorage                                # run this from INSIDE ~/namestorage
cd /opt/namestorage
sudo python3 -m venv venv
sudo venv/bin/pip install -r requirements-agent.txt -r requirements-signaling.txt
sudo cp deploy/agent.example.json /etc/namestorage/agent.json
sudo nano /etc/namestorage/agent.json
```

In `agent.json` set `signaling_url` (the `wss://` address) and `allowed_emails`. Then:

```bash
echo "NAMESTORAGE_SECRET=<same secret as signaling.env>" | sudo tee /etc/namestorage/agent.env
sudo chmod 600 /etc/namestorage/agent.env
sudo chown -R nsagent:nsagent /srv/storage/web
sudo cp deploy/namestorage-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now namestorage-agent
journalctl -u namestorage-agent -f     # expect: connected to signaling server
```

`root` in `agent.json` must be a **dedicated folder**, never your whole disk or home directory.

### 6. Run the signaling server as a service (so it survives reboots)

Edit `signaling.env` the same way, but keep the real one at `/etc/namestorage/signaling.env`:

```bash
sudo cp ~/namestorage/signaling.env /etc/namestorage/signaling.env
sudo chmod 600 /etc/namestorage/signaling.env
sudo cp /opt/namestorage/deploy/namestorage-signaling.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now namestorage-signaling
journalctl -u namestorage-signaling -f     # expect: server listening on 127.0.0.1:8765
```

Stop any manually started copy first, or the port will be busy.

### 7. Turn on the website

In the GitHub repo: **Settings > Pages > Deploy from a branch > `main` > `/docs` > Save.** After a minute or two the site is at `https://<your pages domain>/namestorage/`. With a custom domain on your user site (as here), that is `https://piyushchy.is-a.dev/namestorage/`.

### 8. First test

```bash
echo hello | sudo tee /srv/storage/web/test.txt
sudo chown nsagent:nsagent /srv/storage/web/test.txt
```

Open the site, sign in with the allowed Google account, click Connect, and you should see `test.txt`. Download it, then upload a small file.

---

## Configuration reference

### Signaling server (`signaling.env`, environment variables)

| Variable | Required | Meaning |
|---|---|---|
| `GOOGLE_CLIENT_ID` | yes | OAuth client ID; tokens for any other audience are rejected. |
| `ALLOWED_EMAILS` | yes | Comma-separated emails allowed to connect. |
| `ALLOWED_ORIGINS` | recommended | Comma-separated site origins allowed to open a WebSocket. If empty, any website may use your signaling server (a warning is logged). |
| `NAMESTORAGE_SECRET` | yes | At least 32 characters; identical on the agent. |
| `HOST` / `PORT` | no | Default `127.0.0.1` / `8765`. Keep it on localhost and let Funnel or a proxy expose it. |
| `TRUST_PROXY` | no | `1` when behind Funnel or a proxy that sets `X-Forwarded-For`, so rate limiting sees real client IPs. |

### Agent (`/etc/namestorage/agent.json` and `agent.env`)

| Key | Default | Meaning |
|---|---|---|
| `signaling_url` | required | `wss://` address of the signaling server. |
| `root` | required | Folder to serve. Must exist and not be `/`. |
| `allowed_emails` | required | Emails the agent will accept in tickets. |
| `allow_upload` | `false` if missing | Enables uploads and making folders. |
| `allow_delete` | `false` if missing | Enables deleting files and empty folders. Existing installs must add this line to `/etc/namestorage/agent.json` and restart the agent. |
| `min_free_bytes` | 1 GiB | Uploads are refused if free space would fall below this. |
| `max_sessions` | 3 | Maximum concurrent browser sessions. |
| `session_max_seconds` | 43200 | Maximum session length (12 h). |
| `ice_servers` | Google STUN | List of `stun:` or `turn:` URLs. Must also match the browser's `iceServers` in `docs/config.js`. |
| `NAMESTORAGE_SECRET` (in `agent.env`) | required | Same secret as the signaling server. |

### Website (`docs/config.js`, public)

| Key | Meaning |
|---|---|
| `googleClientId` | OAuth client ID. |
| `signalingUrl` | `wss://` address of the signaling server. |
| `iceServers` | STUN/TURN servers the browser uses. |

---

## Running and maintaining it

```bash
# status and logs
systemctl status namestorage-agent namestorage-signaling
journalctl -u namestorage-agent -f
journalctl -u namestorage-signaling -f
sudo tailscale funnel status

# restart after changing config
sudo systemctl restart namestorage-signaling namestorage-agent

# update the code
cd ~/namestorage && git pull
sudo cp -r . /opt/namestorage            # then restart both services

# change the secret: edit BOTH /etc/namestorage/signaling.env and agent.env, then restart both
```

Where files go: everything under `root` (default `/srv/storage/web`). Interrupted uploads leave nothing visible; hidden `*.nspart` files are cleaned up.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `ModuleNotFoundError: No module named 'namestorage'` when running `pytest` | Pull the latest code (`pytest.ini` sets `pythonpath = .`), or use `python -m pytest`. |
| `cp -r .` copied your whole home folder into `/opt/namestorage` | You ran it from `~`. Remove it with `sudo rm -rf /opt/namestorage`, recreate it, and run `cp` from **inside** the repo folder. |
| Google popup: **Error 400: origin_mismatch** | The site's origin is missing from the OAuth client. Add the exact origin (`https://piyushchy.is-a.dev`, no slash or path) and wait a few minutes. Check which domain the site really loads from (a custom domain on your user site applies to all project pages). |
| Google popup shows no account switcher | It is an error page. Fix the error first. To choose another account, use an InPrivate window, another browser profile, or sign out of other accounts. |
| "this account is not allowed" | The Google account is not in `ALLOWED_EMAILS` (signaling) and/or `allowed_emails` (agent). Fix both and restart. |
| "origin not allowed" | `ALLOWED_ORIGINS` does not contain the page's origin. |
| "login invalid or expired, sign in again" | Token expired (about an hour) or wrong `GOOGLE_CLIENT_ID`. Sign in again; check the ID matches in `config.js` and `signaling.env`. |
| "the storage laptop is offline" | The agent is not connected. `systemctl status namestorage-agent`, check `signaling_url` and that both secrets are identical. |
| Agent log: `agent auth failed` on the signaling server | The two `NAMESTORAGE_SECRET` values differ. |
| "Cannot reach the signaling server" | Signaling service or Funnel is down, or `signalingUrl` is wrong (must be `wss://`). Check `sudo tailscale funnel status` and the service log. |
| "Could not open a direct connection" | Peer-to-peer blocked by your network. Try another network (for example a phone hotspot); for a lasting fix add a TURN relay. |
| `426 Upgrade Required` / `405` lines in the signaling log | Harmless: plain web requests (bots, a browser visiting the URL) rejected by the WebSocket server. |
| Funnel says it is not enabled | Open the link it prints, enable Funnel and HTTPS for the tailnet, run the command again. |
| Google asks for a billing account or card | Google changes its sign-up flow. If a card is required, this app cannot use Google login for free; a password or passkey login would have to replace it. |
| "deleting is disabled on this laptop" | Add `"allow_delete": true` to `/etc/namestorage/agent.json` and run `sudo systemctl restart namestorage-agent`. |
| Upload says "a file with that name already exists" | By design. Uploads never overwrite; rename the file or remove the old one on the laptop. |
| Big download fails in Firefox or Safari | Browser memory limit (no File System Access API). Use Chrome or Edge. |

---

## Tests

```bash
pip install -r requirements-agent.txt -r requirements-signaling.txt pytest pytest-asyncio
pytest
```

25 tests cover path escapes (including symlinks), ticket forgery, expiry and mismatch, and a real WebRTC session on localhost: login, listing, download, a 3 MB chunked upload, refused overwrite, refused bad names, wrong login and wrong origin, upload records, and deleting (including refused escapes, symlinks and non-empty folders).

**Not tested:** real Google tokens, real browsers, and NAT traversal over the real internet.

---

## Repository layout

```
docs/                       website (GitHub Pages): index.html, style.css, app.js, config.js
namestorage/
  signaling.py              signaling server
  agent.py                  laptop agent
  ticket.py                 ticket creation and verification
  paths.py                  path and file-name safety
deploy/
  signaling.env.example     signaling settings template
  agent.example.json        agent settings template
  namestorage-agent.service systemd unit for the agent
  namestorage-signaling.service  systemd unit for the signaling server
tests/                      unit and end-to-end tests
requirements-agent.txt      agent dependencies
requirements-signaling.txt  signaling dependencies
pytest.ini
```

---

## Not included yet

Rename and move, recursive folder delete, a recycle bin, resumable transfers, per-user folders, working share links (the symbol is already in the page), a TURN relay, a login that does not depend on Google.
