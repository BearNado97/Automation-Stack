# Self-Hosted Media Stack (Plex / *arr / VPN / ListenBrainz Sync)

## What this is

This is my personal self-hosted media stack. It does four big things:

1. **Media management**
   - `Plex` serves Movies / Shows / Music to my devices.
   - The `*arr` apps (`Radarr`, `Sonarr`, `Readarr`, `Lidarr`) manage libraries and request/download content.
   - `Prowlarr` centralizes indexers so I don’t have to configure them in each app.
   - `nzbget` and `qBittorrent` actually download media (Usenet + torrents).
   - `autobrr` watches announce channels / indexers and forwards matching releases to my download client.
   - `Tdarr` cleans/transcodes media.
   - `Overseerr` is my request portal.
   - `Tautulli` gives me Plex watch stats.

2. **Network isolation for torrents**
   - `gluetun` creates a VPN tunnel (e.g. NordVPN).
   - `qBittorrent` is forced to run *inside* gluetun’s network namespace.
   - Result: torrent traffic only goes out through the VPN. If the VPN drops, torrent traffic dies instead of leaking out my WAN.

3. **Automated music curation**
   - `lb_sync` is my custom service (see `lb_sync/run.py` + `lb_sync/Dockerfile`).
   - It watches what I play in Plex (usually Plexamp), logs which tracks I like or dislike, and if I dislike a track it uses the Lidarr API to delete that track from disk automatically.
   - It also gives me a tiny local Flask API (`/liked`, `/disliked`, `/now`, `/health`) on port `7000` so I can see what’s happening and poke it manually.

4. **ListenBrainz scrobbling**
   - `plex-beetbrainz` listens for Plex playback and submits “I listened to this” data to ListenBrainz once I’ve played enough of the track.
   - That keeps my ListenBrainz history up to date.

---

## Repo layout

```text
.
├─ docker-compose.yml        # All containers and how they connect
├─ .env.example              # Template env vars (copy this to .env for real use)
├─ lb_sync/
│  ├─ Dockerfile             # Builds the lb_sync container
│  ├─ run.py                 # My "music brain" script
│  └─ config/                # (runtime) Stores liked.json, disliked.json, now_playing.json
└─ README.md                 # You're here
```

In my real deployment, `lb_sync/config/` is mounted as a volume so I don’t lose state if the container restarts.

---

## Prereqs

### 1. Create the Docker network

The compose file expects an external Docker network called `media_net` with a known subnet. I create that once up front:

```bash
docker network create --subnet 172.25.0.0/16 media_net
```

If you prefer a different subnet or name, update the `networks:` block at the bottom of `docker-compose.yml`.

### 2. Copy the example env file

```bash
cp .env.example .env
```

Then open `.env` and fill in all the placeholders:
- `PLEX_URL`, `PLEX_TOKEN`, `PLEX_CLAIM`
- `LB_USER`, `LB_TOKEN`
- `LIDARR_URL`, `LIDARR_API_KEY`
- `VPN_USERNAME`, `VPN_PASSWORD`, `VPN_COUNTRY`
- Paths for `BASE_PATH` and `MEDIA_SHARE`

Do **not** commit `.env` to GitHub. Only commit `.env.example`.

### 3. Make sure the bind mounts exist

Your `.env` defines where configs and media live on the host, e.g.:

- `BASE_PATH=/path/to/appdata`
- `MEDIA_SHARE=/path/to/media`

Before you run `docker compose`, those directories should exist and be writable. For example:

```bash
mkdir -p /path/to/appdata/plex/config
mkdir -p /path/to/appdata/radarr/config
mkdir -p /path/to/appdata/sonarr/config
mkdir -p /path/to/appdata/lidarr/config
mkdir -p /path/to/appdata/prowlarr/config
mkdir -p /path/to/appdata/qbittorrent/config
mkdir -p /path/to/appdata/lb_sync/config
mkdir -p /path/to/media/Downloads
mkdir -p /path/to/media/Plex/Music
```

Match these paths to your actual `.env` values.

---

## Running it

From the same folder as `docker-compose.yml`:

```bash
docker compose up -d
```

What happens:
- Plex, Radarr, Sonarr, Lidarr, etc. all start.
- `gluetun` starts first and establishes the VPN tunnel.
- `qbittorrent` joins gluetun’s network namespace so its traffic is always tunneled.
- `lb_sync` is built from `lb_sync/Dockerfile`, then started.
- `plex-beetbrainz` starts and begins listening for Plex play events.

---

## How `lb_sync` works (the fun/unique part)

### TL;DR behavior
1. I listen to music in Plex (usually via Plexamp).
2. The script in `lb_sync/run.py` polls Plex (`/status/sessions`) to track what I’m currently playing.
3. When playback for a track ends:
   - It waits a short grace period.
   - It fetches final metadata from Plex, including the `userRating` (e.g. “thumbs up” / “thumbs down”).
   - It normalizes that into `like`, `dislike`, or nothing.

4. If I **liked** it:
   - It records that in `liked.json`.

5. If I **disliked** it:
   - It records that in `disliked.json`.
   - It calls Lidarr’s API:
     - searches for that specific track
     - tells Lidarr to delete it from the library (including removing the media file from disk).

This means I can live-curate my library just by hitting “dislike” in Plex. I don’t have to hunt down the file manually.

### State / persistence
Inside the `lb_sync` container, I write these files:
- `config/liked.json`
- `config/disliked.json`
- `config/now_playing.json`

Those are volume-mounted from the host via `docker-compose.yml`, so my “taste memory” survives container restarts.

### Local API
`lb_sync` also runs a tiny Flask app on port `7000`:
- `GET /liked`      → shows everything I’ve liked
- `GET /disliked`   → shows everything I’ve disliked
- `GET /now`        → shows “currently playing” plus last snapshot
- `GET /health`     → simple health/heartbeat

This is super useful for debugging and tapping into the system without attaching a debugger.

> Security note: This little API is meant for local/private LAN access. I don’t expose it to the public internet without auth/reverse proxy.

---

## VPN model (gluetun + qbittorrent)

This is how I stop torrent traffic from ever touching my real WAN directly:

- `gluetun` connects to my VPN provider using credentials I put in `.env` (`VPN_USERNAME`, `VPN_PASSWORD`, etc.`).
- `qbittorrent` uses `network_mode: "service:gluetun"`, which means it literally shares gluetun’s network namespace.
  - So qBittorrent’s network traffic = gluetun’s VPN tunnel.
  - If gluetun dies, qBittorrent basically has no network path. No accidental leak.

---

## Tdarr

`Tdarr` is set up to:
- expose a UI (`8265`)
- run an internal node (`8266`)
- normalize and/or transcode my library automatically so I don’t accumulate weird formats.

In the compose file you’ll see:
```yaml
serverIP=REPLACE_ME_LOCAL_HOST_IP
```
Update that to the LAN IP of the machine running Tdarr so the node can talk to the server.

---

## Overseerr

`Overseerr` (on port `5055`) is what I or future household users will use to request new content. It hooks back into Radarr/Sonarr/Lidarr behind the scenes, so a request in Overseerr becomes an automated download.

---

## Security reminders

- Never commit `.env` with real values.
- Do not expose `lb_sync`'s Flask API publicly unless you add authentication / reverse proxy.
- Your Plex token, ListenBrainz token, Lidarr API key, and VPN credentials are sensitive and should be treated like passwords.

---

## Credit

- Most containers are upstream community images:
  - `linuxserver/*` projects (Plex, Radarr, Sonarr, Lidarr, etc.)
  - `autobrr`
  - `gluetun`
  - `tdarr`
  - `plex-beetbrainz`

- `lb_sync/run.py` and `lb_sync/Dockerfile` are my glue:
  - They sit between Plex, ListenBrainz, and Lidarr to automate curation and scrobbling.
  - Deleting disliked tracks is intentional. Use at your own risk 🙂
