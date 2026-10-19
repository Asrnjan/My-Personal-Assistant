# Deploying for free

The backend is one Python process with a SQLite file. Your phone needs to reach it over HTTPS, and the scheduler needs it running at brief time. Two free ways to do that:

| | Your own computer + Cloudflare Tunnel | Oracle Cloud Always Free VM |
|---|---|---|
| Cost | free | free (Always Free tier) |
| Runs when | your computer is on | always |
| Setup time | about 10 minutes | about 45 minutes |
| Public URL | `https://<name>.trycloudflare.com` or your own domain | same, via the tunnel |

Either way, finish [SETUP.md](SETUP.md) first, then set in `.env`:

```
PUBLIC_BASE_URL=https://assistant.example.com   # your tunnel URL
TRUST_PROXY_HEADERS=true                         # rate limits per real client, not per tunnel
```

and add `<PUBLIC_BASE_URL>/oauth/google/callback` (and/or `/oauth/microsoft/callback`) to your OAuth app's redirect URIs.

## Option 0: a fixed address with only Docker (nothing to install)

The `link` service in `docker-compose.yml` runs Tailscale Funnel inside Docker. It gives a free HTTPS address that never changes, `https://atlas.<your-tailnet>.ts.net`, and it starts by itself with Docker, so no window has to stay open.

1. Sign up (free) at https://login.tailscale.com with your Google or Microsoft account.
2. In the admin console open **DNS** and press **Enable HTTPS**.
3. Open **Settings → Keys → Generate auth key**. Leave Reusable off and Ephemeral off, press **Generate key**, and copy it (`tskey-auth-...`).
4. In `.env` add `TS_AUTHKEY=tskey-auth-...` and `TRUST_PROXY_HEADERS=true`.
5. Run `docker compose --profile link up -d`. After about a minute, **Machines** in the admin console lists `atlas`; its address is the name shown under it plus `.ts.net`, for example `https://atlas.tail1234.ts.net`. `docker compose logs link` also prints it.
6. Set `PUBLIC_BASE_URL` to that address in `.env`, run `docker compose up -d --force-recreate assistant`, and add `<address>/oauth/google/callback` and `/oauth/microsoft/callback` to your OAuth apps.

If the address doesn't open, your tailnet's access policy may not allow Funnel yet: in **Access controls** make sure it contains `"nodeAttrs": [{"target": ["autogroup:member"], "attr": ["funnel"]}]`.

Turn on **Start Docker Desktop when you sign in** (Docker Desktop → Settings → General) and keep the computer from sleeping while plugged in.

## Option 1: Cloudflare Tunnel from your own computer

Cloudflare Tunnel makes an outbound connection to Cloudflare, so you don't open ports on your router, and you get HTTPS for free.

**Try it in one command (temporary URL):**

```bash
# macOS: brew install cloudflared     Windows: winget install --id Cloudflare.cloudflared
# Debian/Ubuntu: see https://pkg.cloudflare.com/
scripts/dev.sh &                      # or: docker compose up -d
cloudflared tunnel --url http://127.0.0.1:8000
```

It prints a `https://<random>.trycloudflare.com` URL. Good for testing; the URL changes every time.

**Keep a fixed URL (free Cloudflare account and a domain on Cloudflare):**

1. Sign up at https://dash.cloudflare.com and add your domain (any registrar; point its nameservers at Cloudflare).
2. **Zero Trust → Networks → Tunnels → Create a tunnel → Cloudflared**. Name it `assistant`.
3. Copy the install command it shows for your OS and run it; or, with Docker, copy just the token into `.env` as `CLOUDFLARE_TUNNEL_TOKEN=...` and run `docker compose --profile tunnel up -d`.
4. **Public Hostname → Add**: subdomain `assistant`, your domain, service `HTTP` → `localhost:8000` (with Docker Compose: `assistant:8000`).
5. Open `https://assistant.<your domain>/health`; it should say `"status":"ok"`.


## Option 2: Oracle Cloud Always Free

1. Sign up at https://www.oracle.com/cloud/free/ (a card is needed for identity; Always Free resources are never charged).
2. **Compute → Instances → Create instance**: image **Ubuntu 24.04**, shape **VM.Standard.A1.Flex** (Ampere, 2 OCPU / 12 GB is within Always Free) or **VM.Standard.E2.1.Micro**. Add your SSH key.
3. SSH in and install the basics:

   ```bash
   sudo apt update && sudo apt install -y git python3-venv ffmpeg
   git clone https://github.com/Asrnjan/My-Personal-Assistant.git && cd My-Personal-Assistant
   scripts/dev.sh        # first run: creates .venv, config files and .env; stop it with Ctrl+C
   nano .env             # add your keys (see SETUP.md)
   ```

4. Run it as a service so it restarts on reboot:

   ```bash
   sudo tee /etc/systemd/system/assistant.service >/dev/null <<UNIT
   [Unit]
   Description=Chief of Staff assistant
   After=network-online.target

   [Service]
   User=$USER
   WorkingDirectory=$HOME/My-Personal-Assistant/backend
   ExecStartPre=$HOME/My-Personal-Assistant/backend/.venv/bin/alembic upgrade head
   ExecStart=$HOME/My-Personal-Assistant/backend/.venv/bin/uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
   Restart=always

   [Install]
   WantedBy=multi-user.target
   UNIT
   sudo systemctl enable --now assistant
   ```

5. Install `cloudflared` on the VM and follow Option 1's fixed-URL steps (no inbound ports needed, so leave Oracle's firewall closed).

The A1 shape has enough memory for the `voice` extra (`pip install -e "backend[voice]"`), so Telegram voice notes and `/stt` work there too.

## Docker Compose (either option)

```bash
cp .env.example .env               # fill it in (SETUP.md)
for f in config/*.example.yaml; do cp -n "$f" "${f/.example/}"; done
docker compose up -d --build       # http://127.0.0.1:8000/
docker compose --profile tunnel up -d   # with CLOUDFLARE_TUNNEL_TOKEN in .env
```

Data (the SQLite database and Piper voices) lives in the `assistant-data` volume. To put voices there: `docker compose exec assistant python -m piper.download_voices --download-dir /data/voices en_US-lessac-medium`.

## Backups and updates

- Everything the assistant knows is in the SQLite file (`backend/assistant.db`, or `/data/assistant.db` in Docker) plus your `config/` and `.env`. Copy those three to back up.
- To update: `git pull`, then restart (`scripts/dev.sh`, `sudo systemctl restart assistant`, or `docker compose up -d --build`). Database migrations run on start.

## If the computer sleeps

The built-in scheduler only runs while the backend runs. If your machine sleeps at brief time, either keep it awake, use the Oracle VM, or drive the jobs from GitHub Actions or cron as described in [SCHEDULING.md](SCHEDULING.md).
