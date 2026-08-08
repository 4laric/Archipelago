# Deploying Peliarch on a Hetzner CX23 (Helsinki, Ubuntu 26.04)

One box runs everything: the **web GUI** (Flask), the **room processes** (stock
MultiServer per room, plus `peliarch` for the Large tier), and **Caddy** in front
for automatic HTTPS on the website. This is the single-node MVP from `HOSTING.md §8`,
donation-model flavor.

Fill in these blanks before you start:

| placeholder | meaning | example |
|---|---|---|
| `YOURDOMAIN` | a domain you control, DNS A-record → the box's IPv4 | `peliarch.ca` |
| `BOXIP` | the server's public IPv4 (Hetzner console) | `65.21.x.x` |
| `archi` | the non-root user we run things as | (keep as-is) |

> **DNS first:** in your domain registrar, add an **A record** for `YOURDOMAIN` → `BOXIP`.
> Caddy needs this resolving before it can issue a TLS cert. Do this now; it can take a
> few minutes to propagate.

---

## 1. First login + a non-root user

```bash
ssh root@BOXIP
adduser --disabled-password --gecos "" archi
usermod -aG sudo archi
# let archi sudo without re-entering a password (optional, convenient):
echo 'archi ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/archi
# copy your SSH key so you can log in as archi:
rsync --archive --chown=archi:archi ~/.ssh /home/archi/
```

From now on log in as `ssh archi@BOXIP`.

## 2. System packages + toolchains

```bash
sudo apt update && sudo apt -y upgrade
sudo apt -y install python3 python3-venv python3-pip git ufw golang-go
go version   # need 1.21+; Ubuntu 26.04 ships newer — fine

# Caddy (official repo)
sudo apt -y install debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt -y install caddy
```

## 3. Get the code onto the box

Either `git clone` your repo, or copy the local folder up from your laptop (PowerShell):

```powershell
scp -r -O .\Archipelago archi@BOXIP:~/Archipelago
```

You need: the AP checkout (`MultiServer.py`, `Utils.py`, etc.), `archipelago-go/`, and
`webgui/`.

## 4. Python env + build the Go server

```bash
cd ~/Archipelago
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt          # AP server deps (restricted_loads, etc.)
pip install flask gunicorn websockets    # web GUI + runtime

# build the Large-tier Go backend (Linux binary, no .exe here)
cd archipelago-go && go build -o peliarch . && cd ..
```

## 5. Firewall + the room port range

Rooms are **port-per-room** (`HOSTING.md §3`): each running room binds one port. Open a
range big enough for your max *concurrent* rooms (a 4 GB box comfortably runs ~10–15
stock rooms at once; 64 ports is plenty of headroom).

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80,443/tcp       # website (HTTP→HTTPS via Caddy)
sudo ufw allow 40000:40063/tcp  # room ports
sudo ufw --force enable
sudo ufw status
```

Tell the orchestrator to allocate inside that same range — set the port-range and the
donation URL in `webgui/` config (see `webgui/README.md` for the exact variable names;
e.g. `DONATION_URL`, the room port range, idle-hibernate timeout, upload size cap).
Until room-level TLS is set up (see §9), have the GUI advertise `ws://` connect addresses,
not `wss://`.

## 6. Caddy: HTTPS for the website

Caddy reverse-proxies the public domain to the Flask app on localhost and gets/renews the
TLS cert automatically. Replace `/etc/caddy/Caddyfile` with:

```caddyfile
YOURDOMAIN {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8000
}
```

```bash
sudo systemctl restart caddy
sudo systemctl status caddy --no-pager
```

Visiting `https://YOURDOMAIN` should now reach Caddy (it'll 502 until the GUI is running — next step).

## 7. Run the web GUI as a service

Create `webgui/wsgi.py` (the WSGI entrypoint gunicorn imports):

```python
from webgui.app import create_app
app = create_app()
```

> If `create_app()` needs arguments in your build, check `webgui/test_app.py` to see how it
> constructs the app and mirror that here. Quick smoke test before making it a service:
> `cd ~/Archipelago && .venv/bin/gunicorn --chdir . webgui.wsgi:app -b 127.0.0.1:8000`
> then `curl -s localhost:8000 | head`.

Create `/etc/systemd/system/peliarch-web.service`:

```ini
[Unit]
Description=Peliarch web GUI
After=network.target

[Service]
User=archi
WorkingDirectory=/home/archi/Archipelago
Environment=DONATION_URL=https://buymeacoffee.com/your-handle
ExecStart=/home/archi/Archipelago/.venv/bin/gunicorn --chdir /home/archi/Archipelago webgui.wsgi:app -b 127.0.0.1:8000 --workers 2 --timeout 120
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now peliarch-web
sudo systemctl status peliarch-web --no-pager
```

Now `https://YOURDOMAIN` should load the dashboard. Upload a `.archipelago`, and the GUI
launches a room on a port in your range; the room page shows the connect address.

## 8. Verify

- `https://YOURDOMAIN` loads, shows the dashboard + the ☕ donation link.
- Upload a generated `.archipelago` → room starts → room page shows a connect address.
- Point an AP client at `ws://YOURDOMAIN:<port>` (the port the room page shows) and connect.
- `sudo journalctl -u peliarch-web -f` tails the GUI logs; `free -h` watches memory (the
  metric that bounds how many rooms run at once).

## 9. (Later) wss on room ports

Desktop AP clients connect over plain `ws://host:port` — fine to launch with. Browser
clients and archipelago.gg require `wss://`. Two ways to add it when you want it:

- **Shared cert:** get a cert for `YOURDOMAIN` (Caddy already has one, or use certbot with a
  DNS challenge for a wildcard), make it readable by `archi`, and have the orchestrator pass
  `--cert fullchain.pem --cert_key privkey.pem` when it launches each MultiServer. Clients
  then use `wss://YOURDOMAIN:<port>`. Flip the GUI's advertised scheme to `wss://`.
- **Dynamic proxy:** drive Caddy's admin API from the orchestrator to add a TLS-terminating
  reverse-proxy listener per room port on start (and remove it on stop). Cleaner long-term,
  more wiring.

Start with `ws://`; add `wss://` once the basic flow is proven.

## 10. Backups

What's stateful: uploaded multidata + the room save files (and `peliarch`'s `--save`
file if you use the Large tier). Point those at one directory and snapshot it:

```bash
# nightly tarball of room state to your home dir (swap in a real offsite target later)
echo '0 4 * * * tar czf ~/backups/rooms-$(date +\%F).tgz -C /home/archi/Archipelago/webgui data' | crontab -
```

For real durability, sync that directory to Hetzner Object Storage or Backblaze B2
(`HOSTING.md §11`).

---

### Cost recap
CX23 Helsinki ≈ **$7.09/mo** (incl. IPv4), 20 TB traffic included — the whole thing
(website + community rooms) runs inside that. Donation jar covers it with room to spare.
Resize to CX33 (8 GB) with a reboot if `free -h` ever shows sustained memory pressure.
