# SpotiDrome

A self-hosted bridge between Spotify / YouTube Music playlists and a [Navidrome](https://www.navidrome.org/) server. SpotiDrome downloads tracks as FLAC via `yt-dlp`, tags them, and rsyncs them straight into your Navidrome music library — keeping playlists in sync on a schedule.

## Features

- **Spotify playlist sync** — authenticate with Spotify, browse your playlists, download any of them as FLAC.
- **Multi-provider matching** — each track is searched against YouTube Music's own "Songs" catalog first (so podcasts/episodes/reactions can never be picked, since YouTube Music itself excludes them from that category), then YouTube Music's "Videos" category, then a plain YouTube search, then SoundCloud — falling through providers until a candidate's title, artist, and duration all plausibly match. Nothing is ever taken as "result #1" on blind faith.
- **YouTube Music support** — paste a track, album, or playlist URL directly.
- **Automatic Navidrome sync** — after each download, triggers a library scan and creates/updates a matching playlist in Navidrome via the Subsonic API.
- **Scheduled auto-sync** — tracked playlists re-sync automatically on a daily or interval schedule, downloading only new tracks.
- **Automatic tag correction** — if a downloaded track's album tag comes back empty or generic ("Unknown Album", or the playlist's own name), SpotiDrome looks up the real album via `yt-dlp` and re-files the track before it ever reaches Navidrome.
- **Real genre tags** — YouTube's embedded metadata labels every music upload's genre as generic "Music"; SpotiDrome looks up the artist's actual genre (e.g. "Metal", "Synthwave") via the Spotify catalog and tags the file with that instead.
- **Failed Downloads page** — a dedicated page listing every track that couldn't be matched or downloaded automatically, with its failure reason. Paste a direct YouTube/YouTube Music/SoundCloud link for any of them to download it manually — it's tagged, filed, and synced to Navidrome exactly like a normal download.
- **Dead-link detection** — a background job periodically checks whether each track's source YouTube video still resolves, and flags any that don't (never auto-deletes).
- **Duplicate cleanup** — a background sweep finds files that trace back to the exact same source video (via an embedded comment tag) and removes the smaller duplicate, then lets Navidrome's own scanner reconcile the change.
- **Permanent track ignore list** — mark a track (e.g. one that's region- or age-restricted and can't be fetched) to be skipped on all future syncs instead of showing up as a repeated failure.
- **Sync health dashboard** — a panel showing last-sync status per tracked playlist, dead-link counts, and duplicate-sweep results.
- **Library cleanup tools** — scan Navidrome for duplicate or overly long tracks and remove them.

## Architecture

- `backend/` — a single-process Flask app (`app.py`) running under gunicorn. All background work (downloads, scheduled sync, dead-link checks, duplicate sweeps) runs as daemon threads inside the same process; state is persisted as flat JSON files.
- `frontend/` — a static single-page UI (vanilla HTML/JS) served by nginx, which proxies `/api/*` to the backend.
- Navidrome itself is **not** part of this stack — SpotiDrome connects to an existing Navidrome instance over SSH (for file transfer) and the Subsonic API (for library/playlist management).

## Setup

1. Copy `.env.example` to `.env` and fill in:
   - `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` / `SPOTIFY_REDIRECT_URI` — from a [Spotify Developer app](https://developer.spotify.com/dashboard).
   - `NAVIDROME_URL` / `NAVIDROME_USER` / `NAVIDROME_PASSWORD` — your Navidrome instance's admin credentials.
   - `SSH_HOST` / `SSH_USER` / `SSH_PORT` / `SSH_MUSIC_PATH` — SSH access to the machine hosting Navidrome's music folder, so downloaded files can be rsynced over.
2. Place an SSH private key (`id_rsa`) authorized on the Navidrome host at `~/.ssh/id_rsa` on the machine running SpotiDrome — it's bind-mounted into the backend container and also doubles as the persistent storage location for the app's runtime state (tracked playlists, job history, schedule config, etc.).
3. Start it with the prebuilt images:
   ```bash
   docker compose pull && docker compose up -d
   ```
   (or build them yourself from this checkout: `docker compose up -d --build`)
4. Open `http://<host>:8080`, log in with a Navidrome **admin** account, authenticate with Spotify, and start tracking playlists.

## Updating

Prebuilt images for `linux/amd64` and `linux/arm64` are published to GitHub Container Registry by `.github/workflows/docker-images.yml`:

| Image | |
|---|---|
| `ghcr.io/lun-astra/spotidrome-backend` | Flask API + download pipeline |
| `ghcr.io/lun-astra/spotidrome-frontend` | nginx + web UI |

- `:latest` is rebuilt on every push to `main` **and every week** from scratch, so security fixes in the base images and the newest yt-dlp / ytmusicapi (YouTube breaks old versions regularly) arrive even when the code hasn't changed.
- `:<version>` / `:<major>.<minor>` exist for release tags (`v1.2.3`) if you'd rather pin; change the tag in `docker-compose.yml`.

To update, from the folder with `docker-compose.yml`:
```bash
docker compose pull && docker compose up -d
```
**Settings → SpotiDrome → Check for updates** tells you whether a newer version is out (and what changed); updating is still this command. Your settings and state are untouched: they live in `.env` and `~/.ssh/` on the host, not in the images. To update automatically, run that on a schedule (cron) or use [Watchtower](https://containrrr.dev/watchtower/); [Diun](https://crazymax.dev/diun/) only notifies.

The images contain no configuration or secrets: `.env`, the SSH key and all runtime state are mounted at runtime (see `.dockerignore`).

## Access & API keys

SpotiDrome can be exposed to the internet: every API route is deny-by-default in `backend/app.py` (`_require_auth`).

- **Web UI:** log in with a Navidrome account that is an **admin** on the destination Navidrome (`NAVIDROME_URL`); the credentials are checked by Navidrome's own `/auth/login`. You get an `HttpOnly`, `SameSite=Lax` session cookie (`Secure` behind https) valid for 30 days. Changes made with that cookie (anything but GET) must also carry `X-SD-Web: 1`, which the pages' shared `auth.js` adds - so another site (even a sibling subdomain, which counts as the same site for cookies) can't make your browser act for you. Log out from Settings → Access. 10 failed logins within 10 minutes lock password login for everyone for 10 minutes; existing sessions and API keys keep working.
- **API keys (apps like LunaDrome):** create one in Settings → Access → API keys. The key (`sdk_…`) is shown **once**; only its SHA-256 hash is stored (`~/.ssh/api_keys.json`). Send it as `Authorization: Bearer <key>` (or `X-API-Key: <key>`). Revoke it there at any time. Each key has a scope:
  - **LunaDrome:** `GET /session`, `GET /search`, `GET /search/album/<browseId>`, `POST /ytmusic/info`, `POST /ytmusic/download`, `GET /jobs`, `GET /jobs/<id>`, `POST /jobs/<id>/skip|cancel`, `GET /ytdlp/version`. Anything else is `403`.
  - **Full access:** everything the web UI can do, except managing API keys (that always needs a web login).
- `GET /session` tells a client who it is (`logged_in`, `via: session|api_key`, `scope`) - handy as a connection test for an API key.
- The backend port (`5000`) is bound to `127.0.0.1` only; everything goes through the frontend's `/api/` proxy. Put a TLS reverse proxy in front of port `8080` for internet access (it should send `X-Forwarded-Proto: https`):
  - Proxy **everything** (one `location /`) to `http://<host>:8080`, and keep the path unchanged: no separate `location /api/` pointing at `:5000` (not reachable from the LAN), and no `proxy_pass http://<host>:8080/;` with a trailing slash inside `location /api/` (that strips `/api`, so API calls get the web page back).
  - Symptom of either: the page loads but shows a red "Can't reach the SpotiDrome API" bar (HTTP 403/502 or 200 with HTML).

## Notes

- Runtime state (tracked playlists, job history, dead-link/duplicate reports, schedule config) lives under `~/.ssh/` on the host, not in this repository.
- **Albums are recognised by ID, not only by name.** Spotify downloads store the Spotify album ID (`SPOTIFY_ALBUM_ID`) and the track's ISRC in the tags. A new track whose album ID is already in the library joins that album under the library's existing name, folder and album artist, even after the album was renamed on Spotify. *Fill in Track Numbers* also adds album IDs to existing tracks it can match.
- **Album names stay consistent with the library.** Streaming services rename and re-spell releases over time (e.g. `X, Vol. 5 (Music from ...)` later becoming `Songs Part Five`, or `Rwby` vs `RWBY`). Before a download picks its album, SpotiDrome reuses an existing album folder whose name only differs in upper/lower case, and applies rename rules from `~/.ssh/album_aliases.json`:
  ```json
  {"Songs Part Five": {"album": "My Album, Vol. 5", "folder": "My Album, Vol. 5", "only_artist": ["Some Artist"]}}
  ```
  Keys are source album names (case-insensitive); `folder` is only needed when an existing folder's name differs from the album tag; `only_artist` limits the rule to tracks whose (album) artist contains one of the names. Renaming happens in the job log as `🏷 Album: … → …`.
- **Library Maintenance** (Settings): each action has a manual button plus an optional weekly automatic run, one per day, all off by default: Mon *Fill in Track Numbers* (adds official track/disc numbers to tracks without them: Spotify, else the YT Music album of the source video; exact album + title matches only, never overwrites), Tue *Relabel Genres*, Wed *Normalize Volume*, Thu *Fix Broken Entries*, Fri *Consolidate Editions*, Sat *Mismatched Tracks* scan. Scheduled runs start after 04:00 UTC (after the default auto-sync) and wait while another job is running. Failed Downloads and Long Tracks stay manual (review pages). Switches are stored in `~/.ssh/maintenance_schedule.json`.
- Spotify downloads are tagged with Spotify's track and disc numbers, so albums play in order. (Tracks from plain YouTube playlists carry no album position, except LunaDrome's whole-album downloads.)
- **LunaDrome integration** ("Download via SpotiDrome" in the LunaDrome player): LunaDrome talks to the same `/api` the web UI uses (`http://<host>:8080/api`). `GET /search?q=` returns YouTube Music songs + albums and plain YouTube videos (Jamidrome's ranking: real releases first); `GET /search/album/<browseId>` returns an album's tracks and the playlist URL to download. Downloads go through `POST /ytmusic/download` with `sync_playlist: false, track_for_sync: false` (library only: no Navidrome playlist, no auto-sync entry), optionally `album` (album name hint) and `job_label`; for a whole album also `complete_album: true` + `album_artist` (every track goes into that album's folder even if it exists elsewhere, e.g. as a single - only a copy already in that folder is skipped - tagged with one album artist and track numbers); progress via `GET /jobs/<id>`. LunaDrome authenticates with an API key (see **Access & API keys**).
