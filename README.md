# SpotiDrome

A self-hosted bridge between Spotify / YouTube Music playlists and a [Navidrome](https://www.navidrome.org/) server. SpotiDrome downloads tracks as FLAC via `yt-dlp`, tags them, and rsyncs them straight into your Navidrome music library — keeping playlists in sync on a schedule.

## Features

- **Spotify playlist sync** — authenticate with Spotify, browse your playlists, download any of them as FLAC.
- **YouTube Music support** — paste a track, album, or playlist URL directly.
- **Automatic Navidrome sync** — after each download, triggers a library scan and creates/updates a matching playlist in Navidrome via the Subsonic API.
- **Scheduled auto-sync** — tracked playlists re-sync automatically on a daily or interval schedule, downloading only new tracks.
- **Automatic tag correction** — if a downloaded track's album tag comes back empty or generic ("Unknown Album", or the playlist's own name), SpotiDrome looks up the real album via `yt-dlp` and re-files the track before it ever reaches Navidrome.
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
3. Build and start:
   ```bash
   docker compose up -d --build
   ```
4. Open `http://<host>:8080`, authenticate with Spotify, and start tracking playlists.

## Notes

- Runtime state (tracked playlists, job history, dead-link/duplicate reports, schedule config) lives under `~/.ssh/` on the host, not in this repository.
