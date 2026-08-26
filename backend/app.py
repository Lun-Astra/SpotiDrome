import os, json, threading, time, re, subprocess, shutil, signal, sys, shlex, difflib
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import requests as http
from flask import Flask, jsonify, request
from flask_cors import CORS
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TCON, COMM, error as ID3Error
from ytmusicapi import YTMusic

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR          = "/downloads"

def _pot_args_for_client(player_client):
    return ["--extractor-args", "youtubepot-bgutilhttp:base_url=http://bgutil-pot:4416",
            "--extractor-args", f"youtube:player_client={player_client}",
            "--remote-components", "ejs:github"]

YTDLP_POT_ARGS        = _pot_args_for_client("mweb")
# Sources (YouTube Music, YouTube, SoundCloud) are mastered at wildly
# different loudness levels, so tracks land in the library at wildly
# different volumes. Every download gets normalized to a single target
# loudness on the way to FLAC via ffmpeg's loudnorm filter, single-pass
# (no separate measure step, since that would mean downloading/decoding
# the audio twice per track). -16 LUFS / -1.5dB true peak / 11 LU range
# are the commonly recommended values for music (vs. -23 LUFS for broadcast).
LOUDNORM_FILTER        = "loudnorm=I=-16:TP=-1.5:LRA=11"
SPOTIFY_CLIENT_ID     = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
SPOTIFY_REDIRECT_URI  = os.environ.get("SPOTIFY_REDIRECT_URI", "http://localhost:8080/callback")
NAVIDROME_CONFIG_FILE = "/root/.ssh/navidrome_config.json"
SSH_CONFIG_FILE       = "/root/.ssh/ssh_config.json"
TRACKED_FILE          = "/root/.ssh/tracked_playlists.json"
SCHEDULE_CONFIG_FILE  = "/root/.ssh/schedule_config.json"
JOBS_FILE             = "/root/.ssh/jobs.json"
DEAD_LINKS_FILE       = "/root/.ssh/dead_links.json"
IGNORED_TRACKS_FILE   = "/root/.ssh/ignored_tracks.json"
DUPLICATE_REPORT_FILE = "/root/.ssh/duplicate_report.json"
TITLE_DUPLICATE_REPORT_FILE = "/root/.ssh/title_duplicate_report.json"
MAX_TITLE_DEDUPE_PER_RUN = 150  # safety cap — see scan_and_dedupe_by_title
SYNC_HEALTH_FILE      = "/root/.ssh/sync_health.json"
FAILED_TRACKS_FILE    = "/root/.ssh/failed_tracks.json"

jobs = {}
job_lock = threading.Lock()
nd_playlist_lock = threading.Lock()

def save_jobs():
    """Write atomically (temp file + rename) so a crash or a concurrent
    reader can never observe a half-written file — that torn read is
    exactly what used to make load_jobs() (see below) fall back to an
    empty dict, which a subsequent save would then happily make permanent."""
    try:
        tmp_path = f"{JOBS_FILE}.tmp"
        with open(tmp_path, "w") as f:
            json.dump(jobs, f)
        os.replace(tmp_path, JOBS_FILE)
    except Exception as e:
        print(f"[jobs] Failed to save {JOBS_FILE}: {e}", file=sys.stderr)

def load_jobs():
    """Only reset to an empty dict when the file genuinely doesn't exist
    yet (first run). If it exists but fails to read/parse, that's a real
    problem — corruption, a torn read, whatever — and silently treating it
    as "no jobs" is how a single bad read used to turn into permanent data
    loss the moment anything next called save_jobs(). Keep whatever's
    already in memory instead and log loudly."""
    global jobs
    if not os.path.exists(JOBS_FILE):
        jobs = {}
        return
    try:
        with open(JOBS_FILE) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"expected a JSON object, got {type(data).__name__}")
        jobs = data
    except Exception as e:
        print(f"[jobs] Failed to load {JOBS_FILE}, keeping in-memory state "
              f"({len(jobs)} job(s)) rather than wiping it: {e}", file=sys.stderr)

load_jobs()

# ─── Tracked playlists ────────────────────────────────────────────────────────

def load_tracked():
    try:
        with open(TRACKED_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_tracked(data):
    with open(TRACKED_FILE, "w") as f:
        json.dump(data, f, indent=2)

def yt_playlist_id(url):
    return f"yt_{re.sub(r'[^a-zA-Z0-9]', '_', url)[:60]}"

# ─── Permanently ignored tracks ─────────────────────────────────────────────

def track_ignore_key(artist, title):
    return f"{(artist or '').strip().lower()}|{(title or '').strip().lower()}"

def load_ignored_tracks():
    try:
        with open(IGNORED_TRACKS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_ignored_tracks(data):
    with open(IGNORED_TRACKS_FILE, "w") as f:
        json.dump(data, f, indent=2)

def is_track_ignored(artist, title):
    return track_ignore_key(artist, title) in load_ignored_tracks()

# ─── Failed downloads (manual-retry queue) ──────────────────────────────────

failed_tracks_lock = threading.Lock()

def load_failed_tracks():
    try:
        with open(FAILED_TRACKS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_failed_tracks(data):
    with open(FAILED_TRACKS_FILE, "w") as f:
        json.dump(data, f, indent=2)

def record_failed_track(track, playlist_id, playlist_name, reason):
    """Remember a track that failed every download attempt, so it can be
    listed on the Failed Downloads page and retried later with a manually
    supplied link. Keyed the same way as the permanent-ignore list, so a
    later successful download or an explicit ignore both naturally
    supersede it."""
    key = track_ignore_key(track.get("artist"), track.get("name"))
    with failed_tracks_lock:
        data = load_failed_tracks()
        data[key] = {
            "artist": track.get("artist"),
            "name": track.get("name"),
            "album": track.get("album"),
            "album_artist": track.get("album_artist"),
            "duration_ms": track.get("duration_ms", 0),
            "playlist_id": playlist_id,
            "playlist_name": playlist_name,
            "reason": reason,
            "last_attempt": datetime.utcnow().isoformat(),
        }
        save_failed_tracks(data)

def clear_failed_track(artist, name):
    key = track_ignore_key(artist, name)
    with failed_tracks_lock:
        data = load_failed_tracks()
        if key in data:
            del data[key]
            save_failed_tracks(data)

def track_playlist(playlist_id, playlist_name, tracks):
    data = load_tracked()
    data[playlist_id] = {
        "id": playlist_id,
        "name": playlist_name,
        "tracks": tracks,
        "last_synced": datetime.utcnow().isoformat(),
    }
    save_tracked(data)

# ─── SSH config ───────────────────────────────────────────────────────────────

def load_ssh_config():
    host = os.environ.get("SSH_HOST", "")
    user = os.environ.get("SSH_USER", "")
    port = os.environ.get("SSH_PORT", "22")
    path = os.environ.get("SSH_MUSIC_PATH", "/opt/navidrome/music")
    if host and user:
        return {"host": host, "user": user, "port": int(port), "music_path": path}
    try:
        with open(SSH_CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return None

def save_ssh_config(host, user, port, music_path):
    with open(SSH_CONFIG_FILE, "w") as f:
        json.dump({"host": host, "user": user, "port": int(port), "music_path": music_path}, f)

def ssh_key_exists():
    return os.path.exists("/root/.ssh/id_rsa")

def generate_ssh_key():
    if not ssh_key_exists():
        subprocess.run(["ssh-keygen", "-t", "rsa", "-b", "4096", "-N", "", "-f", "/root/.ssh/id_rsa"],
                       capture_output=True)

def get_public_key():
    generate_ssh_key()
    try:
        with open("/root/.ssh/id_rsa.pub") as f:
            return f.read().strip()
    except Exception:
        return None

def test_ssh(cfg):
    cmd = ["ssh", "-i", "/root/.ssh/id_rsa",
           "-p", str(cfg["port"]),
           "-o", "StrictHostKeyChecking=no",
           "-o", "ConnectTimeout=8",
           "-o", "BatchMode=yes",
           f"{cfg['user']}@{cfg['host']}", "echo ok"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    return result.returncode == 0, result.stderr.strip()

def get_remote_files(remote_dir, cfg):
    """Get list of all files in a remote directory in one SSH call."""
    try:
        cmd = ["ssh", "-i", "/root/.ssh/id_rsa",
               "-p", str(cfg["port"]),
               "-o", "StrictHostKeyChecking=no",
               "-o", "BatchMode=yes",
               "-o", "ConnectTimeout=5",
               f"{cfg['user']}@{cfg['host']}",
               f"ls '{remote_dir}' 2>/dev/null || echo ''"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return set(result.stdout.strip().split("\n")) if result.stdout.strip() else set()
    except Exception:
        return set()

def get_all_remote_files(cfg):
    """Get ALL filenames (basenames only) across entire music library in one SSH call."""
    try:
        cmd = ["ssh", "-i", "/root/.ssh/id_rsa",
               "-p", str(cfg["port"]),
               "-o", "StrictHostKeyChecking=no",
               "-o", "BatchMode=yes",
               "-o", "ConnectTimeout=5",
               f"{cfg['user']}@{cfg['host']}",
               f"find '{cfg['music_path']}' -name '*.flac' -printf '%f\n' 2>/dev/null"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return set(result.stdout.strip().split("\n")) if result.stdout.strip() else set()
    except Exception:
        return set()

def rsync_to_remote(local_dir, cfg, job_id=None, to_root=True):
    def log(msg):
        if job_id:
            with job_lock:
                jobs[job_id]["log"].append(msg)
    dest = f"{cfg['user']}@{cfg['host']}:{cfg['music_path']}/"
    folder_name = os.path.basename(local_dir)
    target = dest if to_root else f"{dest}{folder_name}/"
    cmd = ["rsync", "-av",
           "-e", f"ssh -i /root/.ssh/id_rsa -p {cfg['port']} -o StrictHostKeyChecking=no -o BatchMode=yes",
           local_dir + "/",
           target]
    log(f"📤 Rsyncing {folder_name} to {cfg['host']}…")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode == 0:
        log("✓ Rsync complete")
        return True
    else:
        log(f"✗ Rsync failed: {result.stderr[-300:]}")
        return False

# ─── Navidrome helpers ────────────────────────────────────────────────────────

def load_nd_config():
    env_url  = os.environ.get("NAVIDROME_URL", "")
    env_user = os.environ.get("NAVIDROME_USER", "")
    env_pass = os.environ.get("NAVIDROME_PASSWORD", "")
    if env_url and env_user and env_pass:
        return {"url": env_url.rstrip("/"), "user": env_user, "password": env_pass}
    try:
        with open(NAVIDROME_CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return None

def save_nd_config(url, user, password):
    with open(NAVIDROME_CONFIG_FILE, "w") as f:
        json.dump({"url": url.rstrip("/"), "user": user, "password": password}, f)

def nd_subsonic(action, cfg=None, **params):
    if cfg is None:
        cfg = load_nd_config()
    if not cfg:
        raise ValueError("Navidrome not configured")
    defaults = {"u": cfg["user"], "p": cfg["password"], "v": "1.16.1", "c": "spotidrome", "f": "json"}
    defaults.update(params)
    resp = http.get(f"{cfg['url']}/rest/{action}", params=defaults, timeout=30)
    resp.raise_for_status()
    data = resp.json().get("subsonic-response", {})
    if data.get("status") != "ok":
        raise ValueError(f"Subsonic error: {data.get('error', {}).get('message', 'unknown')}")
    return data

def nd_get_token(cfg):
    """Get a JWT token from Navidrome REST API."""
    try:
        resp = http.post(f"{cfg['url']}/auth/login",
                        json={"username": cfg["user"], "password": cfg["password"]},
                        timeout=15)
        resp.raise_for_status()
        return resp.json().get("token")
    except Exception as e:
        print(f"[nd_auth] Failed to get token: {e}", file=sys.stderr)
        return None

def nd_trigger_scan(cfg=None, full=False):
    if cfg is None:
        cfg = load_nd_config()
    try:
        resp = http.put(f"{cfg['url']}/api/scanner/trigger",
                        auth=(cfg["user"], cfg["password"]), timeout=15,
                        params={"fullScan": "true"} if full else {})
        resp.raise_for_status()
        return True, "Library scan triggered" + (" (full)" if full else "")
    except Exception as e1:
        try:
            nd_subsonic("startScan", cfg=cfg, fullScan="true" if full else "false")
            return True, "Library scan triggered (subsonic)" + (" full" if full else "")
        except Exception as e2:
            return False, f"Scan failed: {e1} | {e2}"

def nd_wait_for_scan(cfg, timeout=300):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data = nd_subsonic("getScanStatus", cfg=cfg)
            if not data.get("scanStatus", {}).get("scanning", True):
                return True
        except Exception:
            pass
        time.sleep(4)
    return False

def nd_search_songs(query, cfg):
    try:
        data = nd_subsonic("search3", cfg=cfg, query=query, songCount=5, albumCount=0, artistCount=0)
        return data.get("searchResult3", {}).get("song", [])
    except Exception:
        return []

def nd_get_or_create_playlist(name, cfg):
    with nd_playlist_lock:
        last_err = None
        for attempt in range(3):
            try:
                data = nd_subsonic("getPlaylists", cfg=cfg)
                for pl in data.get("playlists", {}).get("playlist", []):
                    if pl["name"].lower() == name.lower():
                        return pl["id"], False
                last_err = None
                break
            except Exception as e:
                last_err = e
                time.sleep(2)
        if last_err is not None:
            raise ValueError(f"Could not list Navidrome playlists: {last_err}")
        data = nd_subsonic("createPlaylist", cfg=cfg, name=name)
        return data.get("playlist", {}).get("id"), True

def nd_sync_playlist(playlist_name, tracks, cfg, job_id=None):
    def log(msg):
        if job_id:
            with job_lock:
                jobs[job_id]["log"].append(msg)
    log("🔍 Matching tracks in Navidrome…")
    song_ids, not_found = [], []
    for t in tracks:
        songs = nd_search_songs(f"{t['artist']} {t['name']}", cfg)
        matched = None
        t_name = t["name"].lower()
        t_artist = t["artist"].lower().split(",")[0].strip()
        for s in songs:
            if (t_name in s.get("title","").lower() or s.get("title","").lower() in t_name) and \
               (t_artist in s.get("artist","").lower() or s.get("artist","").lower() in t_artist):
                matched = s; break
        if not matched and songs:
            matched = songs[0]
        if matched:
            song_ids.append(matched["id"])
        else:
            not_found.append(f"{t['artist']} - {t['name']}")
    if not song_ids:
        log("⚠ No tracks found in Navidrome yet")
        return 0, len(tracks)
    pl_id, created = nd_get_or_create_playlist(playlist_name, cfg)
    existing = nd_subsonic("getPlaylist", cfg=cfg, id=pl_id)
    existing_ids = {e["id"] for e in existing.get("playlist", {}).get("entry", [])}
    new_ids = [sid for sid in dict.fromkeys(song_ids) if sid not in existing_ids]
    for i in range(0, len(new_ids), 50):
        batch = new_ids[i:i+50]
        nd_subsonic("updatePlaylist", cfg=cfg, playlistId=pl_id, songIdToAdd=batch)
    log(f"✅ {'Created' if created else 'Updated'} playlist '{playlist_name}' — {len(new_ids)} new track(s), {len(song_ids)} matched total")
    if not_found:
        log(f"⚠ {len(not_found)} track(s) not matched")
    return len(song_ids), len(not_found)

def batch_upload_and_cleanup(local_dir, ssh_cfg, nd_cfg, playlist_name, batch_tracks, job_id):
    """Rsync current downloads to Navidrome, trigger scan, sync playlist, delete local files."""
    if not os.path.exists(local_dir):
        return
    files = []
    for root, dirs, fnames in os.walk(local_dir):
        for f in fnames:
            if f.endswith('.flac'):
                files.append(os.path.join(root, f))
    if not files:
        return
    with job_lock:
        jobs[job_id]["log"].append(f"📦 Batch uploading {len(files)} files…")
    rsync_ok = rsync_to_remote(local_dir, ssh_cfg, job_id)
    if rsync_ok:
        for f in files:
            try:
                os.remove(f)
            except Exception:
                pass
        with job_lock:
            jobs[job_id]["log"].append("🗑 Batch local files deleted")
        if nd_cfg:
            ok, msg = nd_trigger_scan(nd_cfg)
            with job_lock:
                jobs[job_id]["log"].append(f"🔄 {msg}")
            if ok:
                nd_wait_for_scan(nd_cfg, timeout=120)
            try:
                nd_sync_playlist(playlist_name, batch_tracks, nd_cfg, job_id)
            except Exception as e:
                with job_lock:
                    jobs[job_id]["log"].append(f"⚠ Batch playlist sync error: {e}")

# ─── Spotify ──────────────────────────────────────────────────────────────────

def get_sp():
    auth = SpotifyOAuth(
        client_id=SPOTIFY_CLIENT_ID, client_secret=SPOTIFY_CLIENT_SECRET,
        redirect_uri=SPOTIFY_REDIRECT_URI,
        scope="playlist-read-private playlist-read-collaborative user-library-read",
        cache_path="/root/.ssh/.spotify_cache", open_browser=False)
    token = auth.get_cached_token()
    if not token:
        return None, auth.get_authorize_url()
    if auth.is_token_expired(token):
        try:
            token = auth.refresh_access_token(token["refresh_token"])
        except Exception as e:
            # A transient hiccup here (network blip, momentary 5xx from
            # Spotify) used to propagate as an unhandled exception straight
            # out of every route that calls get_sp() unguarded — surfacing
            # as a raw 500 to the frontend instead of a normal "please
            # reconnect" state. Treat it the same as "not authenticated".
            print(f"[spotify] Token refresh failed: {e}", file=sys.stderr)
            return None, auth.get_authorize_url()
    return spotipy.Spotify(auth=token["access_token"]), None

def fetch_playlist_tracks(sp, playlist_id):
    tracks, offset = [], 0
    while True:
        batch = sp.playlist_tracks(playlist_id, offset=offset, limit=100)
        for item in batch["items"]:
            t = item.get("track")
            if not t or t.get("is_local"): continue
            tracks.append({"id": t["id"], "name": t["name"],
                           "artist": ", ".join(a["name"] for a in t["artists"] if a.get("name")),
                           "album": t["album"]["name"],
                           "album_artist": ", ".join(a["name"] for a in t["album"]["artists"] if a.get("name")),
                           "duration_ms": t["duration_ms"],
                           "image": t["album"]["images"][0]["url"] if t["album"].get("images") else None})
        if not batch["next"]: break
        offset += 100
    return tracks

# ─── Core download helpers ────────────────────────────────────────────────────

def sanitize(name):
    return re.sub(r'[\\/*?:"<>|]', "_", name)

def primary_artist(artist):
    """First name in a comma-joined multi-artist string, for search queries."""
    return artist.split(",")[0].strip()

_genre_cache = {}
_genre_cache_lock = threading.Lock()

# Spotify's artist genre data is real but inconsistently populated — plenty
# of legitimate, popular artists just have an empty genres list, regardless
# of how well-known they are. When that happens, fall back to checking a
# matching YouTube Music upload's own video tags for one that IS a genre
# name — uploaders/labels do sometimes tag their videos with a genre as a
# standalone keyword (e.g. Billie Eilish's official upload is tagged
# "Alternative"). This only matches a *whole* tag exactly, never a substring
# of a longer phrase — freeform tags/descriptions routinely contain ordinary
# English words like "house" or "soul" with nothing to do with genre (e.g.
# a Netflix tie-in tag mentioning "The Fall Of The House Of Usher"), and a
# naive substring search on those is a false-positive machine.
YT_GENRE_KEYWORDS = {
    "drum and bass", "drum n bass", "dnb", "death metal", "black metal",
    "thrash metal", "heavy metal", "nu metal", "metalcore", "deathcore",
    "hard rock", "soft rock", "hip hop", "hip-hop", "r&b", "rnb", "k-pop",
    "j-pop", "new age", "synthwave", "lo-fi", "lofi", "drill", "grime",
    "rock", "pop", "metal", "rap", "soul", "jazz", "blues", "country",
    "folk", "classical", "electronic", "house", "techno", "trance",
    "dubstep", "reggae", "ska", "punk", "indie", "alternative", "grunge",
    "emo", "funk", "disco", "gospel", "ambient", "edm", "garage", "opera",
    "latin", "soundtrack",
}

def lookup_genre_from_youtube(artist):
    """Best-effort fallback genre source when Spotify has nothing for this
    artist: search YouTube Music for them and check whether any of the top
    few results has a video tag that IS a genre name. Much noisier than
    Spotify's own genre taxonomy — most official uploads don't self-tag
    with genre at all — so this often comes up empty too; that's expected,
    not a bug."""
    key = primary_artist(artist).strip()
    if not key:
        return None
    cmd = ["yt-dlp", "--dump-json", "--no-playlist",
           "--default-search", "https://music.youtube.com/search?q=",
           f"ytsearch3:{key}"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        if result.returncode != 0 or not result.stdout.strip():
            return None
    except Exception:
        return None

    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        try:
            info = json.loads(line)
        except Exception:
            continue
        for tag in (info.get("tags") or []):
            normalized = re.sub(r"[^a-z0-9&\- ]", "", tag.lower()).strip()
            if normalized in YT_GENRE_KEYWORDS:
                return normalized.title()
    return None

def lookup_genre(artist):
    """Look up a real, specific genre (e.g. 'Metal', 'Nu Metal', 'Synthwave')
    for an artist — Spotify's catalog first, falling back to YouTube's tags
    when Spotify has nothing — since yt-dlp's embedded YouTube metadata on
    its own just labels every music upload's genre as generic 'Music'.
    Cached per artist name for the life of the process — most playlists hit
    the same artist many times over. Returns None if neither source has
    anything usable.

    Fetches a fresh Spotify client via get_sp() on every call rather than
    taking one as a parameter: a Spotify access token is only valid for
    about an hour, and a long-running job (a big library relabel can run
    for tens of minutes to hours) that grabbed sp once at the start would
    otherwise start silently 401ing partway through — miscounting artists
    Spotify genuinely does have data for as "no genre match". get_sp() is
    cheap when the cached token isn't expired (just a local file read), so
    calling it per-lookup costs nothing in the common case."""
    if not artist:
        return None
    key = primary_artist(artist).lower()
    if not key:
        return None
    with _genre_cache_lock:
        if key in _genre_cache:
            return _genre_cache[key]
    genre = None
    try:
        sp, _ = get_sp()
    except Exception:
        sp = None
    if sp:
        try:
            result = sp.search(q=f"artist:{key}", type="artist", limit=1)
            items = result.get("artists", {}).get("items", [])
            if items:
                genres = items[0].get("genres") or []
                if genres:
                    genre = genres[0].title()
        except Exception as e:
            print(f"[genre] Spotify lookup failed for {artist!r}: {e}", file=sys.stderr)
    if not genre:
        try:
            genre = lookup_genre_from_youtube(artist)
        except Exception as e:
            print(f"[genre] YouTube fallback lookup failed for {artist!r}: {e}", file=sys.stderr)
    with _genre_cache_lock:
        _genre_cache[key] = genre
    return genre

def fix_tags(filepath, title, artist, album, album_artist=None, source_url=None, genre=None):
    album_artist = album_artist or artist
    try:
        if filepath.endswith('.flac'):
            tags = FLAC(filepath)
            tags["title"] = [title]
            tags["artist"] = [artist]
            tags["album"] = [album]
            tags["albumartist"] = [album_artist]
            if source_url:
                tags["comment"] = [source_url]
            if genre:
                tags["genre"] = [genre]
            tags.save()
        else:
            try:
                tags = ID3(filepath)
            except ID3Error:
                tags = ID3()
            tags["TIT2"] = TIT2(encoding=3, text=title)
            tags["TPE1"] = TPE1(encoding=3, text=artist)
            tags["TALB"] = TALB(encoding=3, text=album)
            tags["TPE2"] = TPE2(encoding=3, text=album_artist)
            if source_url:
                tags["COMM"] = COMM(encoding=3, lang="eng", desc="", text=source_url)
            if genre:
                tags["TCON"] = TCON(encoding=3, text=genre)
            tags.save(filepath)
    except Exception as e:
        print(f"Tag fix failed for {filepath}: {e}")

def extract_resolved_url(stdout):
    """Pull the last printed URL line out of yt-dlp's --print output."""
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("http://") or line.startswith("https://"):
            return line
    return None

# ─── Track matching (multi-provider search) ──────────────────────────────────
# Order matters: YouTube Music's own "songs" category is queried first because
# it is YouTube's *own* classification of a result as an actual released track —
# podcasts, episodes, reactions, etc. simply cannot appear there, unlike a plain
# YouTube search or a generic ytsearch against music.youtube.com's search page
# (the old approach), which mixes every content type together and relied on
# fragile title-text heuristics to sort music from everything else.

NOT_MUSIC_KEYWORDS = (
    "podcast", "episode", "interview", "reaction", "react to", "review",
    "breakdown", "explained", "documentary", "trailer", "teaser",
    "behind the scenes", "tier list", "top 10", "top ten", "compilation",
    "let's play", "gameplay", "unboxing", "vlog", "asmr", "full episode",
)

YTMUSIC_OFFICIAL_VIDEO_TYPES = {"MUSIC_VIDEO_TYPE_ATV", "MUSIC_VIDEO_TYPE_OMV"}

_ytmusic_client = None
_ytmusic_disabled = False
# gunicorn runs this as one process with many threads (see Dockerfile:
# --workers 1 --threads 16), so this client — and whatever HTTP session
# ytmusicapi keeps internally — is genuinely shared across every concurrent
# job (a manual sync and the nightly auto-sync, a retry, etc. can all be
# mid-download at once). Unsynchronized concurrent use of a shared
# session/native-extension object like this is a known way to segfault the
# whole process outright rather than raise an ordinary, catchable Python
# exception — confirmed as the actual cause of repeated crashes in
# Jamidrome (which shares this exact pattern, but with a background thread
# hitting it continuously, making the race far more likely to land) — see
# its _ytmusic_lock. Held around both construction and every actual call.
_ytmusic_lock = threading.Lock()

def _get_ytmusic():
    """Lazily construct a shared YTMusic client. If construction ever fails
    (e.g. no network at startup), disable it for the rest of the process
    instead of retrying on every single track."""
    global _ytmusic_client, _ytmusic_disabled
    if _ytmusic_disabled:
        return None
    with _ytmusic_lock:
        if _ytmusic_client is None:
            try:
                _ytmusic_client = YTMusic()
            except Exception as e:
                print(f"[ytmusic] init failed, disabling YT Music search: {e}", file=sys.stderr)
                _ytmusic_disabled = True
                return None
        return _ytmusic_client

def _normalize_title(s):
    s = (s or "").lower()
    s = re.sub(r"\(feat\.?[^)]*\)|\[feat\.?[^\]]*\]", "", s)
    s = re.sub(r"\((remaster(ed)?[^)]*|official[^)]*|lyric[^)]*|audio)\)", "", s)
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _title_ok(candidate_title, expected_title):
    cand_n = _normalize_title(candidate_title)
    exp_n = _normalize_title(expected_title)
    if not exp_n or not cand_n:
        return False
    if exp_n in cand_n:
        return True
    if difflib.SequenceMatcher(None, cand_n, exp_n).ratio() >= 0.72:
        return True
    words = [w for w in exp_n.split() if len(w) > 2]
    return bool(words) and sum(1 for w in words if w in cand_n) / len(words) >= 0.6

def _artist_ok(candidate_names, expected_artist):
    """candidate_names: list of strings that might contain the artist name
    (channel/uploader name, and/or the candidate title itself, since plain
    YouTube uploads often encode the artist only in the title)."""
    expected_tokens = [t.strip().lower() for t in
                        re.split(r",|&|/| x | vs\.? | feat\.?| featuring ", expected_artist or "")
                        if t.strip()]
    if not expected_tokens:
        return True
    haystack = " | ".join(re.sub(r"\s*-\s*topic$", "", (n or "").lower()) for n in candidate_names)
    for tok in expected_tokens:
        if tok in haystack:
            return True
        for part in haystack.split(" | "):
            if part and difflib.SequenceMatcher(None, tok, part).ratio() >= 0.8:
                return True
    return False

def _looks_like_non_music(candidate_title, expected_title):
    cand_l = (candidate_title or "").lower()
    exp_l = (expected_title or "").lower()
    return any(kw in cand_l and kw not in exp_l for kw in NOT_MUSIC_KEYWORDS)

def _duration_close(candidate_sec, expected_sec, pct=0.15, floor=15):
    if not expected_sec:
        return True  # nothing to compare against — don't penalize
    if not candidate_sec:
        return False  # we DO have an expected duration; an unknown one is not "close enough"
    return abs(candidate_sec - expected_sec) <= max(floor, expected_sec * pct)

def _score_ytmusic_entry(entry, expected_title, expected_artist, expected_duration_sec):
    title = entry.get("title") or ""
    artists = [a.get("name", "") for a in (entry.get("artists") or [])]
    duration_sec = entry.get("duration_seconds") or 0
    if _looks_like_non_music(title, expected_title):
        return None
    if not _title_ok(title, expected_title):
        return None
    if not _artist_ok(artists, expected_artist):
        return None
    if not _duration_close(duration_sec, expected_duration_sec):
        return None
    score = difflib.SequenceMatcher(None, _normalize_title(title), _normalize_title(expected_title)).ratio()
    if entry.get("videoType") in YTMUSIC_OFFICIAL_VIDEO_TYPES:
        score += 0.15
    return score

def _score_generic_entry(entry, expected_title, expected_artist, expected_duration_sec):
    title = entry.get("title") or ""
    channel = entry.get("channel") or entry.get("uploader") or ""
    duration_sec = entry.get("duration") or 0
    if _looks_like_non_music(title, expected_title):
        return None
    if not _title_ok(title, expected_title):
        return None
    if not _artist_ok([channel, title], expected_artist):
        return None
    if not _duration_close(duration_sec, expected_duration_sec):
        return None
    return difflib.SequenceMatcher(None, _normalize_title(title), _normalize_title(expected_title)).ratio()

def _search_ytmusic(query, filter_type, expected_title, expected_artist, expected_duration_sec,
                     limit=8, timeout=15):
    ytm = _get_ytmusic()
    if not ytm:
        return None
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        with _ytmusic_lock:  # serialize against every other caller of ytm — see _ytmusic_lock above
            future = executor.submit(ytm.search, query, filter=filter_type, limit=limit)
            results = future.result(timeout=timeout)
    except Exception:
        return None
    finally:
        executor.shutdown(wait=False)

    best, best_score = None, 0.0
    for entry in results or []:
        score = _score_ytmusic_entry(entry, expected_title, expected_artist, expected_duration_sec)
        if score is not None and score > best_score:
            best, best_score = entry, score
    return best

def _search_yt_dlp(search_term, expected_title, expected_artist, expected_duration_sec, timeout=20):
    pot_args = YTDLP_POT_ARGS if search_term.startswith("ytsearch") else []
    cmd = ["yt-dlp", "--dump-json", "--flat-playlist", "--no-playlist"] + pot_args + [search_term]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None

    best, best_score = None, 0.0
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue
        score = _score_generic_entry(entry, expected_title, expected_artist, expected_duration_sec)
        if score is not None and score > best_score:
            url = entry.get("webpage_url") or entry.get("url")
            best, best_score = {"url": url, "title": entry.get("title")}, score
    return best

def iter_track_candidates(track):
    """Yield (url, matched_title, provider_label) candidates across
    providers, in order: YouTube Music songs, YouTube Music videos, plain
    YouTube, SoundCloud. Each provider is only queried once the caller keeps
    asking for more — i.e. once the previous candidate's *download* (not
    just its search match) has failed — so the common case (first candidate
    downloads fine) pays no extra search cost. Each candidate is still
    scored independently on title similarity, artist match, and duration
    closeness before being yielded; nothing is ever taken blindly."""
    expected_title = track.get("name") or ""
    expected_artist = track.get("artist") or ""
    expected_duration_sec = (track.get("duration_ms") or 0) / 1000
    artist_for_query = primary_artist(expected_artist)
    query = f"{artist_for_query} {expected_title}".strip()

    best = _search_ytmusic(query, "songs", expected_title, expected_artist, expected_duration_sec)
    if best:
        yield f"https://music.youtube.com/watch?v={best['videoId']}", best.get("title"), "YouTube Music"

    best = _search_ytmusic(query, "videos", expected_title, expected_artist, expected_duration_sec)
    if best:
        yield f"https://music.youtube.com/watch?v={best['videoId']}", best.get("title"), "YouTube Music (video)"

    best = _search_yt_dlp(f"ytsearch8:{query} audio", expected_title, expected_artist, expected_duration_sec)
    if best:
        yield best["url"], best["title"], "YouTube"

    best = _search_yt_dlp(f"scsearch8:{query}", expected_title, expected_artist, expected_duration_sec)
    if best:
        yield best["url"], best["title"], "SoundCloud"

BAD_ALBUM_VALUES = {"", "unknown album"}

def lookup_real_album(url, timeout=15):
    """Ask yt-dlp for the real album/release of a track, without downloading it."""
    if not url:
        return None
    try:
        cmd = ["yt-dlp", "--dump-json", "--no-playlist", "--skip-download",
               "--socket-timeout", "10", url]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0 or not result.stdout.strip():
            return None
        info = json.loads(result.stdout.strip().split("\n")[0])
        album = (info.get("album") or info.get("release") or "").strip()
        return album or None
    except Exception:
        return None

def maybe_correct_album(flac_path, title, artist, album, playlist_name, source_url, local_dir, album_artist=None):
    """If album looks like a placeholder (empty/'Unknown Album'/the playlist name
    itself), look up the real album via yt-dlp and move the file into the
    corrected album folder. Returns (album, flac_path), updated if corrected."""
    normalized = (album or "").strip().lower()
    if normalized not in BAD_ALBUM_VALUES and normalized != (playlist_name or "").strip().lower():
        return album, flac_path
    real_album = lookup_real_album(source_url)
    if not real_album or real_album.strip().lower() == normalized:
        return album, flac_path
    try:
        new_album_dir = os.path.join(local_dir, sanitize(real_album))
        os.makedirs(new_album_dir, exist_ok=True)
        new_path = os.path.join(new_album_dir, os.path.basename(flac_path))
        if os.path.abspath(new_path) != os.path.abspath(flac_path):
            shutil.move(flac_path, new_path)
        fix_tags(new_path, title, artist, real_album, album_artist=album_artist, source_url=source_url)
        return real_album, new_path
    except Exception as e:
        print(f"Album correction failed for {flac_path}: {e}")
        return album, flac_path

def reap_zombies():
    try:
        while True:
            pid, _ = os.waitpid(-1, os.WNOHANG)
            if pid == 0:
                break
    except ChildProcessError:
        pass

def _extract_yt_dlp_error(stderr):
    """Pull the most useful single-line reason out of yt-dlp's stderr — the
    last 'ERROR:' line if there is one (that's yt-dlp's own summary of why it
    gave up), else the last non-empty line of output. Returns None if stderr
    is empty."""
    if not stderr:
        return None
    lines = [l.strip() for l in stderr.splitlines() if l.strip()]
    if not lines:
        return None
    error_lines = [l for l in lines if l.startswith("ERROR:")]
    reason = error_lines[-1] if error_lines else lines[-1]
    reason = re.sub(r"^ERROR:\s*", "", reason)
    return reason[:200]

def run_yt_dlp(cmd, job_id, label, timeout=30):
    """
    Run a yt-dlp command with:
    - os.nice(15) so gunicorn stays responsive
    - Hard wall-clock deadline
    - Skip flag support
    - Full process group kill on timeout/skip
    Returns (returncode, killed_reason, stdout, stderr) where killed_reason is None on success
    """
    def _set_limits():
        os.setsid()
        os.nice(15)

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            preexec_fn=_set_limits)
    deadline = time.time() + timeout
    killed_reason = None

    while proc.poll() is None:
        # Check skip flag
        with job_lock:
            skip = jobs[job_id].get("skip_current", False)
        if skip:
            killed_reason = "skipped"
            with job_lock:
                jobs[job_id]["skip_current"] = False
            break
        # Check deadline
        if time.time() > deadline:
            killed_reason = "timeout"
            break
        time.sleep(0.3)

    if killed_reason:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        proc.wait()
        reap_zombies()
        return None, killed_reason, "", ""

    # Process finished naturally
    try:
        stdout, stderr = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return None, "timeout", "", ""

    reap_zombies()
    decode = lambda b: b.decode("utf-8", "replace") if isinstance(b, bytes) else (b or "")
    return proc.returncode, None, decode(stdout), decode(stderr)

def _download_via_yt_dlp(video_url, out_template, job_id, label, use_cookies, player_client=None):
    """Run the actual -x flac download for one candidate URL. PO-token
    extractor args are YouTube-specific and meaningless (harmlessly ignored)
    for other extractors, but are gated to YouTube URLs for clarity; cookies
    are opt-in per attempt so callers can retry the same URL without them.
    player_client overrides the default 'mweb' client (see the android-client
    fallback in download_worker for why that's ever needed)."""
    is_youtube_url = "youtube.com" in video_url or "youtu.be" in video_url
    if is_youtube_url:
        pot_args = _pot_args_for_client(player_client) if player_client else YTDLP_POT_ARGS
    else:
        pot_args = []
    cookies_args = ["--cookies", COOKIES_FILE] if (use_cookies and os.path.exists(COOKIES_FILE)) else []
    cmd = ["yt-dlp",
           "-x", "--audio-format", "flac", "--audio-quality", "0",
           "--postprocessor-args", f"ExtractAudio:-af {LOUDNORM_FILTER}",
           "--add-metadata", "--embed-thumbnail", "--output", out_template,
           "--no-playlist",
           "--retries", "1", "--fragment-retries", "1", "--extractor-retries", "1",
           "--concurrent-fragments", "1", "--socket-timeout", "10",
           "--sleep-interval", "2", "--max-sleep-interval", "4",
           "--no-progress", "--print", "before_dl:%(webpage_url)s",
           ] + pot_args + cookies_args + [video_url]
    return run_yt_dlp(cmd, job_id, label, timeout=30)

# ─── Spotify download worker ──────────────────────────────────────────────────

def download_worker(job_id, tracks, playlist_name, playlist_id=None, sync_navidrome=True):
    with job_lock:
        jobs[job_id]["status"] = "running"

    ssh_cfg = load_ssh_config()
    nd_cfg = load_nd_config()
    local_dir = os.path.join(DOWNLOAD_DIR, sanitize(playlist_name))
    os.makedirs(local_dir, exist_ok=True)

    all_synced_tracks = []
    newly_downloaded = []

    # Get ALL remote files once at start (global dedup across all playlists)
    remote_dir = f"{ssh_cfg['music_path']}/{sanitize(playlist_name)}" if ssh_cfg else ""
    remote_files = get_remote_files(remote_dir, ssh_cfg) if ssh_cfg else set()
    remote_files_flat = get_all_remote_files(ssh_cfg) if ssh_cfg else set()

    for i, track in enumerate(tracks):
        with job_lock:
            jobs[job_id]["current"] = i + 1
            jobs[job_id]["current_track"] = f"{track['artist']} - {track['name']}"

        if is_track_ignored(track['artist'], track['name']):
            with job_lock:
                jobs[job_id]["log"].append(f"🚫 Permanently ignored: {track['artist']} - {track['name']}")
            continue

        filename = sanitize(f"{track['artist']} - {track['name']}")
        album_dir = os.path.join(local_dir, sanitize(track['album'] or "Unknown Album"))
        os.makedirs(album_dir, exist_ok=True)
        out_template = os.path.join(album_dir, f"{filename}.%(ext)s")

        # Check remote globally (any folder)
        if any(f.startswith(filename) for f in remote_files_flat):
            with job_lock:
                jobs[job_id]["log"].append(f"⏭ Already on Navidrome: {track['artist']} - {track['name']}")
            all_synced_tracks.append(track)
            continue

        # Check local
        if [f for f in os.listdir(album_dir) if f.startswith(filename)]:
            with job_lock:
                jobs[job_id]["log"].append(f"⏭ Already local: {track['artist']} - {track['name']}")
            newly_downloaded.append(track)
            all_synced_tracks.append(track)
            if len(newly_downloaded) % 50 == 0 and ssh_cfg:
                batch_upload_and_cleanup(local_dir, ssh_cfg, nd_cfg, playlist_name, list(all_synced_tracks), job_id)
                newly_downloaded.clear()
            continue

        # Check skip flag before starting
        with job_lock:
            skip = jobs[job_id].get("skip_current", False)
            if skip:
                jobs[job_id]["skip_current"] = False
                jobs[job_id]["log"].append(f"⏭ Manually skipped: {track['artist']} - {track['name']}")
                jobs[job_id]["failed"] += 1
        if skip:
            continue

        label = f"{track['artist']} - {track['name']}"
        attempts_tried = []
        last_reason = None
        outcome = None  # "success" | "skipped" | None (exhausted)

        for video_url, _matched_title, provider in iter_track_candidates(track):
            attempts_tried.append(provider)
            is_youtube_url = "youtube.com" in video_url or "youtu.be" in video_url
            use_cookies = is_youtube_url and os.path.exists(COOKIES_FILE)

            rc, killed, stdout, stderr = _download_via_yt_dlp(
                video_url, out_template, job_id, label, use_cookies=use_cookies)

            # A stale/mismatched cookies.txt causing an outright 403 is a known
            # failure mode — retry the same candidate once without cookies
            # before giving up on it.
            if use_cookies and killed is None and rc != 0 and "403" in (stderr or ""):
                with job_lock:
                    jobs[job_id]["log"].append(f"↻ Retrying without cookies after 403: {label}")
                rc, killed, stdout, stderr = _download_via_yt_dlp(
                    video_url, out_template, job_id, label, use_cookies=False)

            # YouTube's mweb client is increasingly hit with a probabilistic 403
            # on the actual media fetch, independent of cookies, as part of its
            # ongoing anti-bot enforcement — same video, same everything, just
            # fails sometimes. Last resort before giving up on this candidate:
            # retry via the android client, which reliably routes around it —
            # at the cost of a real quality drop (legacy itag 18, ~96kbps AAC)
            # since android's proper adaptive audio streams are themselves
            # currently blocked by a separate YouTube-side SABR restriction.
            used_android_fallback = False
            if is_youtube_url and killed is None and rc != 0 and "403" in (stderr or ""):
                used_android_fallback = True
                with job_lock:
                    jobs[job_id]["log"].append(f"↻ Retrying via android client (lower quality) after repeated 403: {label}")
                rc, killed, stdout, stderr = _download_via_yt_dlp(
                    video_url, out_template, job_id, label, use_cookies=False, player_client="android")

            if killed == "skipped":
                outcome = "skipped"
                break
            if killed == "timeout":
                last_reason = f"timeout after 30s via {provider}"
                continue
            if rc == 0:
                flac_path = out_template.replace('.%(ext)s', '.flac')
                if not os.path.exists(flac_path):
                    last_reason = f"file missing after download, via {provider}"
                    continue
                source_url = extract_resolved_url(stdout)
                track["source_url"] = source_url
                genre = lookup_genre(track['artist'])
                fix_tags(flac_path, track['name'], track['artist'], track['album'],
                         album_artist=track.get('album_artist'), source_url=source_url, genre=genre)
                new_album, flac_path = maybe_correct_album(
                    flac_path, track['name'], track['artist'], track['album'],
                    playlist_name, source_url, local_dir, album_artist=track.get('album_artist'))
                if new_album != track['album']:
                    with job_lock:
                        jobs[job_id]["log"].append(f"🏷 Corrected album: {track['album']} → {new_album}")
                    track['album'] = new_album
                newly_downloaded.append(track)
                all_synced_tracks.append(track)
                clear_failed_track(track['artist'], track['name'])
                quality_note = " ⚠ lower quality (android fallback)" if used_android_fallback else ""
                with job_lock:
                    jobs[job_id]["log"].append(f"✓ Downloaded via {provider}{quality_note}: {label}")
                    jobs[job_id]["downloaded"] += 1
                if len(newly_downloaded) % 50 == 0 and ssh_cfg:
                    batch_upload_and_cleanup(local_dir, ssh_cfg, nd_cfg, playlist_name, list(all_synced_tracks), job_id)
                    newly_downloaded.clear()
                outcome = "success"
                break
            else:
                last_reason = f"{_extract_yt_dlp_error(stderr) or f'yt-dlp exited with code {rc}'}, via {provider}"
                continue

        if outcome == "skipped":
            with job_lock:
                jobs[job_id]["log"].append(f"⏭ Manually skipped: {label}")
                jobs[job_id]["failed"] += 1
        elif outcome != "success":
            if not attempts_tried:
                reason = "no confident match found on any provider (tried YouTube Music, YouTube, SoundCloud)"
            else:
                reason = f"failed after {len(attempts_tried)} source(s) tried [{', '.join(attempts_tried)}] — {last_reason}"
            record_failed_track(track, playlist_id, playlist_name, reason)
            with job_lock:
                if not attempts_tried:
                    jobs[job_id]["log"].append(f"✗ No confident match found (tried YouTube Music, YouTube, SoundCloud): {label}")
                else:
                    jobs[job_id]["log"].append(
                        f"✗ Failed after {len(attempts_tried)} source(s) tried "
                        f"[{', '.join(attempts_tried)}] — last error: {last_reason}: {label}")
                jobs[job_id]["failed"] += 1

    if playlist_id and all_synced_tracks:
        track_playlist(playlist_id, playlist_name, tracks)
        print(f"[track] Saved Spotify playlist: {playlist_name} ({playlist_id})", file=sys.stderr)
    elif not playlist_id:
        print(f"[track] No playlist_id provided for {playlist_name}", file=sys.stderr)
    elif not all_synced_tracks:
        print(f"[track] all_synced_tracks empty for {playlist_name}", file=sys.stderr)

    # Final rsync for remaining files
    if newly_downloaded and ssh_cfg:
        with job_lock:
            jobs[job_id]["status"] = "uploading"
            jobs[job_id]["current_track"] = f"Uploading remaining tracks…"
        rsync_ok = rsync_to_remote(local_dir, ssh_cfg, job_id)
        if rsync_ok:
            try:
                shutil.rmtree(local_dir)
                with job_lock:
                    jobs[job_id]["log"].append("🗑 Local temp files deleted")
            except Exception as e:
                with job_lock:
                    jobs[job_id]["log"].append(f"⚠ Cleanup failed: {e}")
    elif not newly_downloaded:
        with job_lock:
            jobs[job_id]["log"].append("ℹ All tracks already on Navidrome")

    # Final Navidrome scan + playlist sync
    if sync_navidrome and nd_cfg and all_synced_tracks:
        with job_lock:
            jobs[job_id]["status"] = "scanning"
            jobs[job_id]["current_track"] = "Triggering Navidrome library scan…"
        ok, msg = nd_trigger_scan(nd_cfg)
        with job_lock:
            jobs[job_id]["log"].append(f"🔄 {msg}")
        if ok:
            with job_lock:
                jobs[job_id]["current_track"] = "Waiting for scan to finish…"
            nd_wait_for_scan(nd_cfg, timeout=300)
        with job_lock:
            jobs[job_id]["current_track"] = "Syncing playlist in Navidrome…"
        try:
            added, missing = nd_sync_playlist(playlist_name, all_synced_tracks, nd_cfg, job_id)
            with job_lock:
                jobs[job_id]["nd_synced"] = added
                jobs[job_id]["nd_missing"] = missing
        except Exception as e:
            with job_lock:
                jobs[job_id]["log"].append(f"⚠ Playlist sync error: {e}")

    with job_lock:
        jobs[job_id]["status"] = "done"
        jobs[job_id]["current_track"] = None
        downloaded_n, failed_n, total_n = jobs[job_id]["downloaded"], jobs[job_id]["failed"], jobs[job_id]["total"]
        save_jobs()
    record_sync_health(playlist_id, downloaded_n, failed_n, total_n)

# ─── YouTube Music worker ─────────────────────────────────────────────────────

def ytmusic_get_info(url):
    cmd = ["yt-dlp", "--dump-json", "--flat-playlist",
           "--no-playlist" if "watch?v=" in url and "list=" not in url else "--yes-playlist",
           url]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise ValueError(f"yt-dlp error: {result.stderr[-300:]}")
    entries = []
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except Exception:
            pass
    return entries

def ytmusic_download_worker(job_id, url, playlist_name, is_playlist=False):
    with job_lock:
        jobs[job_id]["status"] = "running"

    ssh_cfg = load_ssh_config()
    nd_cfg = load_nd_config()
    local_dir = os.path.join(DOWNLOAD_DIR, sanitize(playlist_name))
    os.makedirs(local_dir, exist_ok=True)

    with job_lock:
        jobs[job_id]["current_track"] = "Fetching track list from YouTube Music…"
    try:
        entries = ytmusic_get_info(url)
    except Exception as e:
        with job_lock:
            jobs[job_id]["log"].append(f"✗ Failed to fetch info: {e}")
            jobs[job_id]["status"] = "done"
            jobs[job_id]["current_track"] = None
        if is_playlist:
            record_sync_health(yt_playlist_id(url), 0, 0, 0, status="failed")
        return

    with job_lock:
        jobs[job_id]["total"] = len(entries)
        jobs[job_id]["log"].append(f"ℹ Found {len(entries)} track(s)")

    downloaded_tracks = []
    yt_track_list = []

    # Get ALL remote files once (global dedup across all playlists)
    remote_dir = f"{ssh_cfg['music_path']}/{sanitize(playlist_name)}" if ssh_cfg else ""
    remote_files = get_remote_files(remote_dir, ssh_cfg) if ssh_cfg else set()
    remote_files_flat = get_all_remote_files(ssh_cfg) if ssh_cfg else set()

    for i, entry in enumerate(entries):
        track_id = entry.get("id") or entry.get("url", "")
        title = entry.get("title", "Unknown Title")
        artist = entry.get("uploader") or entry.get("channel") or entry.get("artist") or "Unknown Artist"
        artist = re.sub(r" - Topic$", "", artist)
        album = entry.get("album") or entry.get("release") or "Unknown Album"
        track_url = f"https://www.youtube.com/watch?v={track_id}" if not track_id.startswith("http") else track_id

        with job_lock:
            jobs[job_id]["current"] = i + 1
            jobs[job_id]["current_track"] = f"{artist} - {title}"

        if is_track_ignored(artist, title):
            with job_lock:
                jobs[job_id]["log"].append(f"🚫 Permanently ignored: {artist} - {title}")
            continue

        filename = sanitize(f"{artist} - {title}")
        album_dir = os.path.join(local_dir, sanitize(album or "Unknown Album"))
        os.makedirs(album_dir, exist_ok=True)
        out_template = os.path.join(album_dir, f"{filename}.%(ext)s")
        t = {"id": track_id, "name": title, "artist": artist, "album": album, "duration_ms": 0, "image": None, "source_url": track_url}

        # Check remote globally (any folder)
        if any(f.startswith(filename) for f in remote_files_flat):
            with job_lock:
                jobs[job_id]["log"].append(f"⏭ Already on Navidrome: {artist} - {title}")
            downloaded_tracks.append(t)
            yt_track_list.append(t)
            continue

        # Check local
        if [f for f in os.listdir(album_dir) if f.startswith(filename)]:
            with job_lock:
                jobs[job_id]["log"].append(f"⏭ Already local: {artist} - {title}")
            downloaded_tracks.append(t)
            yt_track_list.append(t)
            if len(downloaded_tracks) % 50 == 0 and ssh_cfg:
                batch_upload_and_cleanup(local_dir, ssh_cfg, nd_cfg, playlist_name, list(downloaded_tracks), job_id)
            continue

        # Check skip flag
        with job_lock:
            skip = jobs[job_id].get("skip_current", False)
            if skip:
                jobs[job_id]["skip_current"] = False
                jobs[job_id]["log"].append(f"⏭ Manually skipped: {artist} - {title}")
                jobs[job_id]["failed"] += 1
        if skip:
            continue

        def _yt_music_download_cmd(use_cookies, player_client=None):
            cookies_args = ["--cookies", COOKIES_FILE] if (use_cookies and os.path.exists(COOKIES_FILE)) else []
            pot_args = _pot_args_for_client(player_client) if player_client else YTDLP_POT_ARGS
            return ["yt-dlp",
                    "-x", "--audio-format", "flac", "--audio-quality", "0",
                    "--add-metadata", "--embed-thumbnail",
                    "--output", out_template,
                    "--no-playlist" if not is_playlist else "--yes-playlist",
                    "--retries", "1", "--fragment-retries", "1", "--extractor-retries", "1",
                    "--concurrent-fragments", "1", "--socket-timeout", "10",
                    "--sleep-interval", "2", "--max-sleep-interval", "4",
                    "--no-progress",
                    ] + pot_args + cookies_args + [track_url]

        used_cookies = os.path.exists(COOKIES_FILE)
        rc, killed, _stdout, stderr = run_yt_dlp(
            _yt_music_download_cmd(used_cookies), job_id, f"{artist} - {title}", timeout=30)

        # A stale/mismatched cookies.txt causing an outright 403 is a known
        # failure mode — retry once without cookies before giving up.
        if used_cookies and killed is None and rc != 0 and "403" in (stderr or ""):
            with job_lock:
                jobs[job_id]["log"].append(f"↻ Retrying without cookies after 403: {artist} - {title}")
            rc, killed, _stdout, stderr = run_yt_dlp(
                _yt_music_download_cmd(False), job_id, f"{artist} - {title}", timeout=30)

        # Same probabilistic mweb 403 as the Spotify path — last resort before
        # giving up, retry via the android client (lower audio quality, but
        # far more likely to succeed; see download_worker for the full story).
        used_android_fallback = False
        if killed is None and rc != 0 and "403" in (stderr or ""):
            used_android_fallback = True
            with job_lock:
                jobs[job_id]["log"].append(f"↻ Retrying via android client (lower quality) after repeated 403: {artist} - {title}")
            rc, killed, _stdout, stderr = run_yt_dlp(
                _yt_music_download_cmd(False, player_client="android"), job_id, f"{artist} - {title}", timeout=30)

        failed_track_stub = {"artist": artist, "name": title, "album": album,
                              "album_artist": None, "duration_ms": 0}
        failed_playlist_id = yt_playlist_id(url) if is_playlist else None

        if killed == "skipped":
            with job_lock:
                jobs[job_id]["log"].append(f"⏭ Manually skipped: {artist} - {title}")
                jobs[job_id]["failed"] += 1
        elif killed == "timeout":
            record_failed_track(failed_track_stub, failed_playlist_id, playlist_name, "timeout after 30s")
            with job_lock:
                jobs[job_id]["log"].append(f"✗ Timeout (30s): {artist} - {title}")
                jobs[job_id]["failed"] += 1
        elif rc == 0:
            flac_path = out_template.replace(".%(ext)s", ".flac")
            if not os.path.exists(flac_path):
                record_failed_track(failed_track_stub, failed_playlist_id, playlist_name, "file missing after download")
                with job_lock:
                    jobs[job_id]["log"].append(f"✗ Failed (file missing after download): {artist} - {title}")
                    jobs[job_id]["failed"] += 1
                continue
            genre = lookup_genre(artist)
            fix_tags(flac_path, title, artist, album, source_url=track_url, genre=genre)
            new_album, flac_path = maybe_correct_album(
                flac_path, title, artist, album, playlist_name, track_url, local_dir)
            if new_album != album:
                with job_lock:
                    jobs[job_id]["log"].append(f"🏷 Corrected album: {album} → {new_album}")
                album = new_album
                t["album"] = new_album
            downloaded_tracks.append(t)
            yt_track_list.append(t)
            clear_failed_track(artist, title)
            quality_note = " ⚠ lower quality (android fallback)" if used_android_fallback else ""
            with job_lock:
                jobs[job_id]["log"].append(f"✓ Downloaded{quality_note}: {artist} - {title}")
                jobs[job_id]["downloaded"] += 1
            if len(downloaded_tracks) % 50 == 0 and ssh_cfg:
                batch_upload_and_cleanup(local_dir, ssh_cfg, nd_cfg, playlist_name, list(downloaded_tracks), job_id)
        else:
            reason = _extract_yt_dlp_error(stderr) or f"yt-dlp exited with code {rc}"
            record_failed_track(failed_track_stub, failed_playlist_id, playlist_name, reason)
            with job_lock:
                jobs[job_id]["log"].append(f"✗ Failed ({reason}): {artist} - {title}")
                jobs[job_id]["failed"] += 1

    if is_playlist and yt_track_list:
        url_id = yt_playlist_id(url)
        data = load_tracked()
        data[url_id] = {
            "id": url_id,
            "name": playlist_name,
            "url": url,
            "tracks": yt_track_list,
            "last_synced": datetime.utcnow().isoformat(),
        }
        save_tracked(data)
        with job_lock:
            jobs[job_id]["log"].append(f"📌 Tracked for auto-sync")

    # Final rsync
    remaining = []
    if os.path.exists(local_dir):
        for _r, _d, _f in os.walk(local_dir):
            remaining.extend(x for x in _f if x.endswith('.flac'))
    if remaining and ssh_cfg:
        with job_lock:
            jobs[job_id]["status"] = "uploading"
            jobs[job_id]["current_track"] = f"Uploading remaining tracks…"
        rsync_ok = rsync_to_remote(local_dir, ssh_cfg, job_id)
        if rsync_ok:
            try:
                shutil.rmtree(local_dir)
                with job_lock:
                    jobs[job_id]["log"].append("🗑 Local temp files deleted")
            except Exception as e:
                with job_lock:
                    jobs[job_id]["log"].append(f"⚠ Cleanup failed: {e}")
    elif not remaining:
        pass

    # Final Navidrome scan + playlist sync
    if nd_cfg and downloaded_tracks:
        with job_lock:
            jobs[job_id]["status"] = "scanning"
            jobs[job_id]["current_track"] = "Triggering Navidrome library scan…"
        ok, msg = nd_trigger_scan(nd_cfg)
        with job_lock:
            jobs[job_id]["log"].append(f"🔄 {msg}")
        if ok:
            with job_lock:
                jobs[job_id]["current_track"] = "Waiting for scan to finish…"
            nd_wait_for_scan(nd_cfg, timeout=300)
        with job_lock:
            jobs[job_id]["current_track"] = "Syncing playlist in Navidrome…"
        try:
            added, missing = nd_sync_playlist(playlist_name, downloaded_tracks, nd_cfg, job_id)
            with job_lock:
                jobs[job_id]["nd_synced"] = added
                jobs[job_id]["nd_missing"] = missing
        except Exception as e:
            with job_lock:
                jobs[job_id]["log"].append(f"⚠ Playlist sync error: {e}")

    with job_lock:
        jobs[job_id]["status"] = "done"
        jobs[job_id]["current_track"] = None
        downloaded_n, failed_n, total_n = jobs[job_id]["downloaded"], jobs[job_id]["failed"], jobs[job_id]["total"]
        save_jobs()
    if is_playlist:
        record_sync_health(yt_playlist_id(url), downloaded_n, failed_n, total_n)

# ─── Auto-sync scheduler ──────────────────────────────────────────────────────

def auto_sync_worker():
    print("[auto-sync] Starting nightly sync…")
    tracked = load_tracked()
    if not tracked:
        print("[auto-sync] No tracked playlists to sync.")
    else:
        try:
            sp, _ = get_sp()
        except Exception as e:
            print(f"[auto-sync] Spotify auth failed, skipping Spotify playlists this run: {e}")
            sp = None

        for playlist_id, info in tracked.items():
            playlist_name = info["name"]
            print(f"[auto-sync] Syncing: {playlist_name}")
            job_id = f"auto_{int(time.time()*1000)}"

            # YouTube playlist (id starts with yt_)
            if playlist_id.startswith("yt_"):
                # Reconstruct original URL from stored tracks
                url = info.get("url")
                if not url:
                    print(f"[auto-sync] No URL stored for {playlist_name}, skipping.")
                    continue
                with job_lock:
                    jobs[job_id] = {"id": job_id, "playlist": f"[Auto] {playlist_name}",
                                    "status": "pending", "total": 0, "current": 0,
                                    "downloaded": 0, "failed": 0, "nd_synced": None,
                                    "nd_missing": None, "current_track": None, "log": []}
                ytmusic_download_worker(job_id, url, playlist_name, is_playlist=True)
            else:
                # Spotify playlist
                if not sp:
                    print("[auto-sync] Spotify not authenticated, skipping Spotify playlists.")
                    continue
                try:
                    tracks = fetch_playlist_tracks(sp, playlist_id)
                except Exception as e:
                    print(f"[auto-sync] Failed to fetch tracks for {playlist_name}: {e}")
                    continue
                with job_lock:
                    jobs[job_id] = {"id": job_id, "playlist": f"[Auto] {playlist_name}",
                                    "status": "pending", "total": len(tracks), "current": 0,
                                    "downloaded": 0, "failed": 0, "nd_synced": None,
                                    "nd_missing": None, "current_track": None, "log": []}
                download_worker(job_id, tracks, playlist_name, playlist_id=playlist_id, sync_navidrome=True)

            time.sleep(5)

    print("[auto-sync] Running nightly title/artist duplicate sweep…")
    try:
        scan_and_dedupe_by_title(load_ssh_config(), load_nd_config())
    except Exception as e:
        print(f"[auto-sync] Title duplicate sweep failed: {e}", file=sys.stderr)

    print("[auto-sync] Nightly sync complete.")

def load_schedule_config():
    try:
        with open(SCHEDULE_CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {"enabled": True, "mode": "time", "hour": 3, "interval_hours": 24}

def save_schedule_config(cfg):
    with open(SCHEDULE_CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

def seconds_until_next_run(cfg):
    from datetime import timedelta
    now = datetime.utcnow()
    if cfg.get("mode") == "interval":
        hours = int(cfg.get("interval_hours", 24))
        return hours * 3600
    else:
        hour = int(cfg.get("hour", 3))
        target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        return (target - now).total_seconds()

def scheduler_loop():
    while True:
        cfg = load_schedule_config()
        if not cfg.get("enabled", True):
            print("[scheduler] Auto-sync disabled, checking again in 60s")
            time.sleep(60)
            continue
        wait_seconds = seconds_until_next_run(cfg)
        print(f"[scheduler] Next auto-sync in {wait_seconds/3600:.1f} hours")
        time.sleep(wait_seconds)
        cfg = load_schedule_config()
        if cfg.get("enabled", True):
            threading.Thread(target=auto_sync_worker, daemon=True).start()

threading.Thread(target=scheduler_loop, daemon=True).start()

# ─── Dead-link detection (flag only, never auto-deletes) ──────────────────────

def load_dead_links():
    try:
        with open(DEAD_LINKS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_dead_links(data):
    with open(DEAD_LINKS_FILE, "w") as f:
        json.dump(data, f, indent=2)

def _url_is_dead(url):
    try:
        cmd = ["yt-dlp", "--skip-download", "--simulate", "--socket-timeout", "10",
               "-q", "--no-warnings", url]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return result.returncode != 0
    except Exception:
        return True

def check_dead_links():
    print("[dead-link] Starting dead-link check…")
    tracked = load_tracked()
    report = {}
    for playlist_id, info in tracked.items():
        dead = []
        for t in info.get("tracks", []):
            url = t.get("source_url")
            if not url:
                continue
            if _url_is_dead(url):
                dead.append({
                    "track_id": t.get("id"), "name": t.get("name"),
                    "artist": t.get("artist"), "url": url,
                    "checked_at": datetime.utcnow().isoformat(),
                })
            time.sleep(2)
        if dead:
            report[playlist_id] = dead
    save_dead_links(report)
    total = sum(len(v) for v in report.values())
    print(f"[dead-link] Done — {total} dead link(s) across {len(report)} playlist(s)")

def dead_link_loop():
    time.sleep(300)  # let the app settle after startup before the first pass
    while True:
        try:
            check_dead_links()
        except Exception as e:
            print(f"[dead-link] Error: {e}", file=sys.stderr)
        time.sleep(7 * 24 * 3600)

threading.Thread(target=dead_link_loop, daemon=True).start()

# ─── Duplicate sweep (remote, exact video-id match, auto-delete) ──────────────

def load_duplicate_report():
    try:
        with open(DUPLICATE_REPORT_FILE) as f:
            return json.load(f)
    except Exception:
        return {"last_run": None, "removed": [], "error": None}

def save_duplicate_report(data):
    with open(DUPLICATE_REPORT_FILE, "w") as f:
        json.dump(data, f, indent=2)

_DEDUPE_REMOTE_SCRIPT = r'''
import json, os, re, sys
from mutagen.flac import FLAC

MUSIC = sys.argv[1]
VIDEO_ID_RE = re.compile(r"(?:v=|youtu\.be/)([\w-]{11})")

groups = {}
for root, dirs, files in os.walk(MUSIC):
    for f in files:
        if not f.endswith(".flac"):
            continue
        p = os.path.join(root, f)
        try:
            tags = FLAC(p)
            comment = tags.get("comment", [""])[0]
            m = VIDEO_ID_RE.search(comment)
            if not m:
                continue
            vid = m.group(1)
            groups.setdefault(vid, []).append({"path": p, "size": os.path.getsize(p)})
        except Exception:
            pass

dupes = {vid: files for vid, files in groups.items() if len(files) > 1}
print(json.dumps(dupes))
'''

def scan_and_dedupe_remote(ssh_cfg, nd_cfg):
    """Find files sharing the same source YouTube video ID (from the comment tag)
    and delete all but the largest. File deletion only — never touches the
    Navidrome sqlite DB directly; a scan afterward lets Navidrome's own scanner
    reconcile its index."""
    if not ssh_cfg:
        report = {"last_run": datetime.utcnow().isoformat(), "removed": [], "error": "SSH not configured"}
        save_duplicate_report(report)
        return report

    cmd = ["ssh", "-i", "/root/.ssh/id_rsa", "-p", str(ssh_cfg["port"]),
           "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
           f"{ssh_cfg['user']}@{ssh_cfg['host']}",
           f"python3 -c {shlex.quote(_DEDUPE_REMOTE_SCRIPT)} {shlex.quote(ssh_cfg['music_path'])}"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if result.returncode != 0:
            report = {"last_run": datetime.utcnow().isoformat(), "removed": [], "error": result.stderr[-300:]}
            save_duplicate_report(report)
            return report
        groups = json.loads(result.stdout.strip() or "{}")
    except Exception as e:
        report = {"last_run": datetime.utcnow().isoformat(), "removed": [], "error": str(e)}
        save_duplicate_report(report)
        return report

    removed = []
    for vid, files in groups.items():
        files.sort(key=lambda f: f["size"], reverse=True)
        for f in files[1:]:
            rm_cmd = ["ssh", "-i", "/root/.ssh/id_rsa", "-p", str(ssh_cfg["port"]),
                      "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
                      f"{ssh_cfg['user']}@{ssh_cfg['host']}",
                      f"rm -f -- {repr(f['path'])}"]
            rm_result = subprocess.run(rm_cmd, capture_output=True, text=True, timeout=15)
            if rm_result.returncode == 0:
                removed.append({"path": f["path"], "size": f["size"], "video_id": vid})

    if removed and nd_cfg:
        # A full scan alone isn't enough here — this Navidrome only ever
        # flags a vanished file's row as missing=1, it doesn't delete it
        # (that's why /cleanup/delete above resorts to a direct SQL DELETE
        # too). Reuse that same proven approach: find every row whose file
        # is actually gone and delete the row outright, which is what
        # actually clears the "husk" left behind by the rm above.
        try:
            prune_orphaned_navidrome_entries(ssh_cfg, nd_cfg)
        except Exception as e:
            print(f"[dedupe] Orphan prune after delete failed: {e}", file=sys.stderr)

    report = {"last_run": datetime.utcnow().isoformat(), "removed": removed, "error": None}
    save_duplicate_report(report)
    print(f"[dedupe] Removed {len(removed)} duplicate file(s)")
    return report

def duplicate_scan_loop():
    time.sleep(3600)  # offset from dead_link_loop's initial delay
    while True:
        try:
            scan_and_dedupe_remote(load_ssh_config(), load_nd_config())
        except Exception as e:
            print(f"[dedupe] Error: {e}", file=sys.stderr)
        time.sleep(7 * 24 * 3600)

threading.Thread(target=duplicate_scan_loop, daemon=True).start()

# ─── Title/artist duplicate sweep (nightly, part of the 3am auto-sync) ────────
# The dedupe above only catches files that share the exact same *source
# video* (via the embedded comment tag) — it can't see two files of the
# same song that came from two different YouTube uploads (e.g. one synced
# normally through a playlist, one requested through Jamidrome from a
# different video of the same track). This sweep instead reads every
# file's own TITLE/ARTIST tags, groups by normalized title, and within
# each group clusters by fuzzy artist similarity — catching duplicates
# regardless of which video they were sourced from.

def load_title_duplicate_report():
    try:
        with open(TITLE_DUPLICATE_REPORT_FILE) as f:
            data = json.load(f)
            data.setdefault("history", [])
            return data
    except Exception:
        return {"last_run": None, "removed": [], "error": None, "history": []}

def save_title_duplicate_report(data):
    with open(TITLE_DUPLICATE_REPORT_FILE, "w") as f:
        json.dump(data, f, indent=2)

def _artist_somewhat_matches(a, b, threshold=0.85):
    """Deliberately strict — this gates an *unattended* nightly delete, so a
    false positive here means silently losing a real, distinct song. A raw
    substring check (dropped from an earlier version) let short/generic
    artist names match all sorts of unrelated collaborators; this now only
    accepts a whole-word match (every word of the shorter name appears as a
    whole word in the longer one) or a high overall similarity ratio."""
    a_n = re.sub(r"[^\w\s]", " ", (a or "").lower()).strip()
    b_n = re.sub(r"[^\w\s]", " ", (b or "").lower()).strip()
    a_n = re.sub(r"\s+", " ", a_n)
    b_n = re.sub(r"\s+", " ", b_n)
    if not a_n or not b_n:
        return False
    if a_n == b_n:
        return True
    a_words, b_words = set(a_n.split()), set(b_n.split())
    shorter, longer = (a_words, b_words) if len(a_words) <= len(b_words) else (b_words, a_words)
    if shorter and shorter.issubset(longer):
        return True
    return difflib.SequenceMatcher(None, a_n, b_n).ratio() >= threshold

def _duration_somewhat_matches(a, b, tolerance_sec=12, tolerance_pct=0.1):
    """Extra corroborating signal for the title/artist dedupe sweep — two
    files can share a normalized title and a plausible artist match while
    still being genuinely different recordings (a short intro/reprise with
    the same name as the full track, a different edit, etc.). Missing
    duration data (0 or absent, e.g. an old scan before this field existed)
    never blocks a match on its own — it just means duration adds nothing
    for that pair."""
    if not a or not b:
        return True
    diff = abs(a - b)
    return diff <= tolerance_sec or diff <= max(a, b) * tolerance_pct

_TITLE_DEDUPE_SCAN_SCRIPT = r'''
import json, os, sys
import mutagen
from mutagen.flac import FLAC
from mutagen.id3 import ID3, error as ID3Error

MUSIC = sys.argv[1]
out = []
for root, dirs, files in os.walk(MUSIC):
    for f in files:
        p = os.path.join(root, f)
        try:
            if f.endswith(".flac"):
                tags = FLAC(p)
                title = (tags.get("title") or [""])[0]
                artist = (tags.get("artist") or [""])[0]
            elif f.endswith(".mp3"):
                tags = ID3(p)
                title = str(tags.get("TIT2", ""))
                artist = str(tags.get("TPE1", ""))
            else:
                continue
            size = os.path.getsize(p)
            try:
                duration = mutagen.File(p).info.length
            except Exception:
                duration = 0
        except Exception:
            continue
        if title and artist:
            out.append({"path": p, "title": title, "artist": artist, "size": size, "duration": duration})
print(json.dumps(out))
'''

def scan_and_dedupe_by_title(ssh_cfg, nd_cfg):
    """Removes the smaller file(s) from each title+artist(+duration)
    duplicate cluster found across the whole library, then adds each
    removed track's own artist/title to the permanent ignore list — same
    as a user manually marking it ignored — so a future playlist sync or
    jam request doesn't just re-download the very duplicate just removed.

    2026-08-22: after this ran unattended for a couple of nights, a
    library audit found real, distinct songs among the removed tracks
    (the artist-match threshold was too loose) *and* every one of those
    deletions left an orphaned Navidrome entry behind (a "husk" — a
    library row pointing at a file that no longer exists), because the
    scan triggered after deleting was a regular scan rather than a full
    one — and it turns out even a full scan only flags a vanished file's
    row as missing=1 here rather than deleting it. Both are fixed below:
    the artist match is stricter and now corroborated by duration, and
    the post-delete cleanup directly prunes the now-orphaned row(s)
    instead of counting on any scan to do it."""
    prior = load_title_duplicate_report()
    history = prior.get("history", [])

    if not ssh_cfg:
        report = {"last_run": datetime.utcnow().isoformat(), "removed": [], "error": "SSH not configured", "history": history}
        save_title_duplicate_report(report)
        return report

    cmd = ["ssh", "-i", "/root/.ssh/id_rsa", "-p", str(ssh_cfg["port"]),
           "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
           f"{ssh_cfg['user']}@{ssh_cfg['host']}",
           f"python3 -c {shlex.quote(_TITLE_DEDUPE_SCAN_SCRIPT)} {shlex.quote(ssh_cfg['music_path'])}"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            report = {"last_run": datetime.utcnow().isoformat(), "removed": [], "error": result.stderr[-300:], "history": history}
            save_title_duplicate_report(report)
            return report
        files = json.loads(result.stdout.strip() or "[]")
    except Exception as e:
        report = {"last_run": datetime.utcnow().isoformat(), "removed": [], "error": str(e), "history": history}
        save_title_duplicate_report(report)
        return report

    by_title = {}
    for f in files:
        key = _normalize_title(f["title"])
        if key:
            by_title.setdefault(key, []).append(f)

    # Build the full removal plan before touching anything — this runs
    # unattended every night with no human review, so a safety cap on how
    # much a single run can ever remove matters more here than in a
    # manually-triggered sweep. A few dozen is a perfectly normal first-run
    # backlog (two download tools writing into the same library will
    # accumulate some overlap); anything far beyond that in one night is
    # more likely a bug than a real duplicate wave, and should stop for a
    # human to look rather than silently mass-delete.
    plan = []
    for group in by_title.values():
        if len(group) < 2:
            continue
        # Cluster within this title group by fuzzy artist match *and*
        # duration — same title with a different artist, or a
        # suspiciously different length, is never merged. Same title +
        # plausible artist alone isn't enough; two of the three
        # (title/artist/duration) agreeing loosely is how the last round
        # of this let real, distinct songs get merged and deleted.
        clusters = []
        for f in group:
            for cluster in clusters:
                anchor = cluster[0]
                if (_artist_somewhat_matches(f["artist"], anchor["artist"]) and
                        _duration_somewhat_matches(f.get("duration", 0), anchor.get("duration", 0))):
                    cluster.append(f)
                    break
            else:
                clusters.append([f])

        for cluster in clusters:
            if len(cluster) < 2:
                continue
            cluster.sort(key=lambda x: x["size"], reverse=True)
            keep, dupes = cluster[0], cluster[1:]
            for d in dupes:
                plan.append((keep, d))

    if len(plan) > MAX_TITLE_DEDUPE_PER_RUN:
        report = {"last_run": datetime.utcnow().isoformat(), "removed": [],
                   "error": f"Safety cap hit: {len(plan)} would be removed in one run "
                            f"(max {MAX_TITLE_DEDUPE_PER_RUN}) — skipped entirely for a human to check first.",
                   "history": history}
        save_title_duplicate_report(report)
        print(f"[title-dedupe] {report['error']}", file=sys.stderr)
        return report

    removed = []
    for keep, d in plan:
        rm_cmd = ["ssh", "-i", "/root/.ssh/id_rsa", "-p", str(ssh_cfg["port"]),
                  "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
                  f"{ssh_cfg['user']}@{ssh_cfg['host']}",
                  f"rm -f -- {shlex.quote(d['path'])}"]
        rm_result = subprocess.run(rm_cmd, capture_output=True, text=True, timeout=15)
        if rm_result.returncode != 0:
            continue
        entry = {"path": d["path"], "size": d["size"], "title": d["title"],
                 "artist": d["artist"], "kept": keep["path"], "removed_at": datetime.utcnow().isoformat()}
        removed.append(entry)
        history.append(entry)
        key = track_ignore_key(d["artist"], d["title"])
        ignored = load_ignored_tracks()
        ignored[key] = {"artist": d["artist"], "title": d["title"],
                         "added_at": datetime.utcnow().isoformat(),
                         "reason": f"Auto-removed as a duplicate of {keep['path']}"}
        save_ignored_tracks(ignored)

    if removed and nd_cfg:
        # A full scan alone doesn't do it — this Navidrome only flags a
        # vanished file's row as missing=1 rather than deleting it (same
        # reason /cleanup/delete above resorts to a direct SQL DELETE).
        # Reuse that proven approach: find every row whose file is
        # actually gone and delete the row outright.
        try:
            prune_orphaned_navidrome_entries(ssh_cfg, nd_cfg)
        except Exception as e:
            print(f"[title-dedupe] Orphan prune after delete failed: {e}", file=sys.stderr)

    report = {"last_run": datetime.utcnow().isoformat(), "removed": removed, "error": None, "history": history}
    save_title_duplicate_report(report)
    print(f"[title-dedupe] Removed {len(removed)} duplicate(s) by title/artist/duration match, added to ignore list")
    return report

# ─── Genre relabeling (retag existing library in place) ────────────────────
# Runs entirely on the Navidrome host over SSH, the same way the dedupe sweep
# above does — no file is ever transferred, moved, or re-downloaded. Only the
# GENRE/TCON tag of files that need it gets rewritten in place; everything
# else about the library (paths, playlists, other tags, the audio itself)
# is untouched.

_GENRE_SCAN_REMOTE_SCRIPT = r'''
import json, os, sys
from mutagen.flac import FLAC
from mutagen.id3 import ID3, error as ID3Error

MUSIC = sys.argv[1]
out = []
for root, dirs, files in os.walk(MUSIC):
    for f in files:
        p = os.path.join(root, f)
        try:
            if f.endswith(".flac"):
                tags = FLAC(p)
                artist = (tags.get("artist") or [""])[0]
                genre = (tags.get("genre") or [""])[0]
            elif f.endswith(".mp3"):
                tags = ID3(p)
                artist = str(tags.get("TPE1", ""))
                genre = str(tags.get("TCON", ""))
            else:
                continue
        except Exception:
            continue
        out.append({"path": p, "artist": artist, "genre": genre})
print(json.dumps(out))
'''

_GENRE_APPLY_REMOTE_SCRIPT = r'''
import json, sys
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TCON, error as ID3Error

updates = json.load(sys.stdin)
updated, failed = 0, []
for item in updates:
    p, genre = item["path"], item["genre"]
    try:
        if p.endswith(".flac"):
            tags = FLAC(p)
            tags["genre"] = [genre]
            tags.save()
        elif p.endswith(".mp3"):
            try:
                tags = ID3(p)
            except ID3Error:
                tags = ID3()
            tags["TCON"] = TCON(encoding=3, text=genre)
            tags.save(p)
        else:
            continue
        updated += 1
    except Exception:
        failed.append(p)
print(json.dumps({"updated": updated, "failed": failed}))
'''

def _ssh_cmd(ssh_cfg, remote_command):
    return ["ssh", "-i", "/root/.ssh/id_rsa", "-p", str(ssh_cfg["port"]),
            "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
            f"{ssh_cfg['user']}@{ssh_cfg['host']}", remote_command]

# ─── Orphaned Navidrome entries ("husks") ──────────────────────────────────
# A file deleted straight off disk (by either dedupe sweep above, or by hand)
# leaves Navidrome's own database row behind unless a *full* scan runs
# afterward — both sweeps now do that going forward, but this cleans up
# whatever was already left behind by a run from before that fix, or by
# anything else that ever removed a file without triggering one.
_ORPHAN_SCAN_REMOTE_SCRIPT = r'''
import sys
music = sys.argv[1]
for line in sys.stdin:
    line = line.rstrip("\n")
    if not line:
        continue
    id_, path = line.split("\x01", 1)
    full = path if path.startswith("/") else f"{music}/{path}"
    import os
    if not os.path.isfile(full):
        print(id_)
'''

def find_orphaned_navidrome_entries(ssh_cfg):
    """Returns [{'id':..., 'path':...}] for every media_file row whose file
    no longer exists on disk. Read-only — deletes nothing."""
    # SQLite's own char(1) inserts the delimiter directly in the query
    # output, rather than relying on a shell-quoted -separator flag that
    # would depend on the remote's login shell supporting ANSI-C quoting.
    select_cmd = _ssh_cmd(ssh_cfg,
        "sqlite3 /var/lib/navidrome/navidrome.db "
        "\"SELECT id || char(1) || path FROM media_file;\"")
    result = subprocess.run(select_cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-300:] or "sqlite3 select failed")
    rows = {}
    for line in result.stdout.splitlines():
        if "\x01" not in line:
            continue
        id_, path = line.split("\x01", 1)
        rows[id_] = path

    filter_cmd = _ssh_cmd(ssh_cfg,
        f"python3 -c {shlex.quote(_ORPHAN_SCAN_REMOTE_SCRIPT)} {shlex.quote(ssh_cfg['music_path'])}")
    result = subprocess.run(filter_cmd, input=result.stdout, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-300:] or "orphan filter failed")
    missing_ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return [{"id": i, "path": rows.get(i, "")} for i in missing_ids]

def prune_orphaned_navidrome_entries(ssh_cfg, nd_cfg):
    orphans = find_orphaned_navidrome_entries(ssh_cfg)
    if not orphans:
        return {"pruned": 0, "entries": [], "error": None}

    ids_str = ",".join(f"'{o['id']}'" for o in orphans)
    del_cmd = _ssh_cmd(ssh_cfg,
        f"sqlite3 /var/lib/navidrome/navidrome.db \"DELETE FROM media_file WHERE id IN ({ids_str});\"")
    result = subprocess.run(del_cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return {"pruned": 0, "entries": [], "error": result.stderr[-300:]}

    if nd_cfg:
        ok, _msg = nd_trigger_scan(nd_cfg, full=True)
        if ok:
            nd_wait_for_scan(nd_cfg, timeout=300)

    return {"pruned": len(orphans), "entries": orphans, "error": None}

def _navidrome_has_close_match(title, artist, nd_cfg):
    """True if Navidrome already has a song that's plausibly this same
    title+artist, under whatever tags it actually has (which can differ
    from the exact string being searched for — that's precisely how the
    title/artist dedupe sweep above found it as a "duplicate" in the
    first place). Used by the dedupe-undo recovery below so it doesn't
    just re-download a fresh copy of something that already has a
    surviving copy under slightly different tags."""
    queries = {_normalize_title(title), f"{primary_artist(artist)} {title}", title}
    seen_ids, candidates = set(), []
    for q in queries:
        if not q.strip():
            continue
        try:
            data = nd_subsonic("search3", cfg=nd_cfg, query=q, songCount=25, albumCount=0, artistCount=0)
        except Exception:
            continue
        for s in data.get("searchResult3", {}).get("song", []):
            if s.get("id") not in seen_ids:
                seen_ids.add(s.get("id"))
                candidates.append(s)
    for s in candidates:
        if _title_ok(s.get("title", ""), title) and _artist_ok([s.get("artist", "")], artist):
            return True
    return False

def genre_relabel_worker(job_id):
    with job_lock:
        jobs[job_id]["status"] = "running"

    ssh_cfg = load_ssh_config()
    nd_cfg = load_nd_config()
    if not ssh_cfg:
        with job_lock:
            jobs[job_id]["log"].append("✗ SSH not configured")
            jobs[job_id]["status"] = "done"
        return
    try:
        sp, _ = get_sp()
    except Exception:
        sp = None
    if not sp:
        with job_lock:
            jobs[job_id]["log"].append("✗ Spotify not authenticated — can't look up genres")
            jobs[job_id]["status"] = "done"
        return

    with job_lock:
        jobs[job_id]["current_track"] = "Scanning library on the Navidrome host…"
    scan_cmd = _ssh_cmd(ssh_cfg, f"python3 -c {shlex.quote(_GENRE_SCAN_REMOTE_SCRIPT)} "
                                  f"{shlex.quote(ssh_cfg['music_path'])}")
    try:
        result = subprocess.run(scan_cmd, capture_output=True, text=True, timeout=180)
        if result.returncode != 0:
            raise ValueError(result.stderr[-500:])
        files = json.loads(result.stdout.strip() or "[]")
    except Exception as e:
        with job_lock:
            jobs[job_id]["log"].append(f"✗ Failed to scan library: {e}")
            jobs[job_id]["status"] = "done"
        return

    with job_lock:
        jobs[job_id]["total"] = len(files)
        jobs[job_id]["log"].append(f"ℹ Scanned {len(files)} file(s) on the Navidrome host")

    # Group by artist — each unique artist only hits the Spotify lookup once
    # regardless of how many of their tracks are in the library.
    by_artist = {}
    for f in files:
        by_artist.setdefault(f["artist"], []).append(f)

    updates, processed, already_correct, no_genre_found = [], 0, 0, 0
    for artist, group in by_artist.items():
        processed += len(group)
        with job_lock:
            jobs[job_id]["current"] = processed
            jobs[job_id]["current_track"] = f"Looking up genre for {artist or '(unknown artist)'}…"
            skip = jobs[job_id].get("skip_current", False)
            if skip:
                jobs[job_id]["skip_current"] = False
        if skip:
            with job_lock:
                jobs[job_id]["log"].append(f"⏭ Skipped: {artist or '(unknown artist)'}")
            continue

        new_genre = lookup_genre(artist)
        if not new_genre:
            no_genre_found += len(group)
            continue
        for f in group:
            if (f.get("genre") or "").strip().lower() == new_genre.lower():
                already_correct += 1
                continue
            updates.append({"path": f["path"], "genre": new_genre})

    with job_lock:
        jobs[job_id]["log"].append(
            f"ℹ {len(updates)} file(s) need a genre update, {already_correct} already correct, "
            f"{no_genre_found} had no genre match on Spotify")

    applied, failed = 0, 0
    if updates:
        with job_lock:
            jobs[job_id]["current_track"] = f"Writing {len(updates)} genre tag(s) on the Navidrome host…"
        BATCH = 200
        for i in range(0, len(updates), BATCH):
            batch = updates[i:i + BATCH]
            apply_cmd = _ssh_cmd(ssh_cfg, f"python3 -c {shlex.quote(_GENRE_APPLY_REMOTE_SCRIPT)}")
            try:
                result = subprocess.run(apply_cmd, input=json.dumps(batch),
                                         capture_output=True, text=True, timeout=120)
                if result.returncode != 0:
                    raise ValueError(result.stderr[-500:])
                summary = json.loads(result.stdout.strip() or "{}")
                applied += summary.get("updated", 0)
                failed += len(summary.get("failed", []))
            except Exception as e:
                failed += len(batch)
                with job_lock:
                    jobs[job_id]["log"].append(f"✗ Batch write failed ({len(batch)} file(s)): {e}")
                continue
            with job_lock:
                jobs[job_id]["current"] = min(len(files), i + BATCH)
                jobs[job_id]["downloaded"] = applied
                jobs[job_id]["failed"] = failed
                jobs[job_id]["log"].append(f"🏷 Retagged {applied}/{len(updates)} so far…")

    with job_lock:
        jobs[job_id]["log"].append(f"✅ Done: {applied} genre tag(s) updated, {failed} failed")

    if applied and nd_cfg:
        with job_lock:
            jobs[job_id]["status"] = "scanning"
            jobs[job_id]["current_track"] = "Triggering Navidrome library scan…"
        ok, msg = nd_trigger_scan(nd_cfg)
        with job_lock:
            jobs[job_id]["log"].append(f"🔄 {msg}")
        if ok:
            nd_wait_for_scan(nd_cfg, timeout=300)

    with job_lock:
        jobs[job_id]["current"] = len(files)  # apply-phase batching only tracks len(updates), not the full scan total
        jobs[job_id]["status"] = "done"
        jobs[job_id]["current_track"] = None
        save_jobs()

# ─── Volume normalization (existing library) ───────────────────────────────
# New downloads are already normalized on the way in (see LOUDNORM_FILTER /
# _download_via_yt_dlp), but that does nothing for tracks that were
# downloaded before that existed. This sweeps the whole library on the
# Navidrome host over SSH — one file per SSH round trip, same as a genre
# lookup — and only touches files whose measured loudness actually falls
# outside the target, re-encoding those in place while preserving every
# tag and embedded picture exactly as they were.
#
# Unlike the download-time normalization (single-pass, to avoid decoding
# each track twice over the network), this runs measure-then-apply as two
# real ffmpeg passes per file that needs it — decoding a file that's
# already local to the Navidrome host is effectively free, and the two-pass
# form is the more accurate way to hit the target loudness.
_LOUDNORM_TARGET_I = float(re.search(r"I=(-?[\d.]+)", LOUDNORM_FILTER).group(1))
_LOUDNORM_TOLERANCE_LU = 1.0  # skip files already within 1 LU of the target

_LOUDNORM_ONE_FILE_SCRIPT = f'''
import json, os, subprocess, sys, tempfile

path = sys.argv[1]
FILTER = {LOUDNORM_FILTER!r}
TARGET_I = {_LOUDNORM_TARGET_I}
TOLERANCE = {_LOUDNORM_TOLERANCE_LU}

def done(action, **extra):
    print(json.dumps({{"action": action, **extra}}))
    sys.exit(0)

def measure():
    cmd = ["ffmpeg", "-i", path, "-af", FILTER + ":print_format=json", "-vn", "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    start = r.stderr.rfind("{{")
    end = r.stderr.find("}}", start) if start != -1 else -1
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(r.stderr[start:end + 1])
    except Exception:
        return None

ext = os.path.splitext(path)[1].lower()
if ext not in (".flac", ".mp3"):
    done("skipped", reason="unsupported format")

summary = measure()
if summary is None:
    done("failed", reason="loudness measurement failed")

try:
    input_i = float(summary.get("input_i", "0"))
except Exception:
    input_i = 0.0

if input_i == float("-inf") or abs(input_i - TARGET_I) <= TOLERANCE:
    done("skipped", lufs=input_i)

if ext == ".flac":
    from mutagen.flac import FLAC
    orig = FLAC(path)
    pictures, vc = orig.pictures, (dict(orig.tags) if orig.tags else {{}})
    codec_args = ["-c:a", "flac"]
else:
    from mutagen.id3 import ID3
    try:
        orig_id3 = ID3(path)
    except Exception:
        orig_id3 = None
    codec_args = ["-c:a", "libmp3lame", "-q:a", "0"]

fd, tmp_path = tempfile.mkstemp(suffix=ext, dir=os.path.dirname(path))
os.close(fd)
try:
    cmd = (["ffmpeg", "-y", "-i", path, "-af", FILTER, "-map_metadata", "-1", "-vn"]
           + codec_args + [tmp_path])
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=280)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-300:])
    if ext == ".flac":
        new_tags = FLAC(tmp_path)
        for k, v in vc.items():
            new_tags[k] = v
        for pic in pictures:
            new_tags.add_picture(pic)
        new_tags.save()
    elif orig_id3 is not None:
        orig_id3.save(tmp_path)
    # tempfile.mkstemp() deliberately creates its file mode 0600 (owner-only)
    # — a sane default for an actual temp file, but this one is about to
    # BECOME the real library file via the os.replace() below, and
    # os.replace()/rename() does not change permission bits. Left as-is,
    # every successfully-normalized file ends up owner-read-only — unreadable
    # by whatever user Navidrome's own service actually runs as (not
    # necessarily root, and wasn't here), which is exactly what made ~74% of
    # a real library unplayable after a normalize run. Match the ORIGINAL
    # file's mode rather than hardcoding one, so this keeps working
    # correctly regardless of whatever permission convention a given
    # library/host actually uses.
    os.chmod(tmp_path, os.stat(path).st_mode)
    os.replace(tmp_path, path)
    done("normalized", lufs_before=input_i)
except Exception as e:
    try:
        os.unlink(tmp_path)
    except Exception:
        pass
    done("failed", reason=str(e)[:200])
'''

def volume_normalize_worker(job_id):
    with job_lock:
        jobs[job_id]["status"] = "running"

    ssh_cfg = load_ssh_config()
    nd_cfg = load_nd_config()
    if not ssh_cfg:
        with job_lock:
            jobs[job_id]["log"].append("✗ SSH not configured")
            jobs[job_id]["status"] = "done"
        return

    with job_lock:
        jobs[job_id]["current_track"] = "Listing library files on the Navidrome host…"
    list_cmd = _ssh_cmd(ssh_cfg,
        f"find {shlex.quote(ssh_cfg['music_path'])} "
        f"\\( -iname '*.flac' -o -iname '*.mp3' \\) -type f -printf '%P\\n'")
    try:
        result = subprocess.run(list_cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise ValueError(result.stderr[-500:])
        rel_paths = [p for p in result.stdout.splitlines() if p.strip()]
    except Exception as e:
        with job_lock:
            jobs[job_id]["log"].append(f"✗ Failed to list library: {e}")
            jobs[job_id]["status"] = "done"
        return

    with job_lock:
        jobs[job_id]["total"] = len(rel_paths)
        jobs[job_id]["log"].append(f"ℹ Found {len(rel_paths)} file(s) on the Navidrome host")

    normalized = failed = skipped = 0
    for i, rel_path in enumerate(rel_paths):
        remote_path = f"{ssh_cfg['music_path']}/{rel_path}"
        with job_lock:
            jobs[job_id]["current"] = i + 1
            jobs[job_id]["current_track"] = rel_path
            skip = jobs[job_id].get("skip_current", False)
            if skip:
                jobs[job_id]["skip_current"] = False
        if skip:
            skipped += 1
            with job_lock:
                jobs[job_id]["log"].append(f"⏭ Skipped: {rel_path}")
            continue

        cmd = _ssh_cmd(ssh_cfg, f"python3 -c {shlex.quote(_LOUDNORM_ONE_FILE_SCRIPT)} "
                                 f"{shlex.quote(remote_path)}")
        # run_yt_dlp is a generic "run this subprocess with skip/timeout
        # supervision" helper despite the name — reused as-is here rather
        # than renaming it just for this caller.
        rc, killed, stdout, stderr = run_yt_dlp(cmd, job_id, rel_path, timeout=300)

        if killed == "skipped":
            skipped += 1
            with job_lock:
                jobs[job_id]["log"].append(f"⏭ Skipped: {rel_path}")
            continue
        if killed == "timeout":
            failed += 1
            with job_lock:
                jobs[job_id]["log"].append(f"✗ Timed out: {rel_path}")
            continue

        try:
            start = stdout.rfind("{")
            evt = json.loads(stdout[start:]) if start != -1 else {}
        except Exception:
            evt = {}
        action = evt.get("action")

        if action == "normalized":
            normalized += 1
            lb = evt.get("lufs_before")
            lb_str = f"{lb:.1f} LUFS" if isinstance(lb, (int, float)) else "?"
            with job_lock:
                jobs[job_id]["log"].append(f"🔊 Normalized: {rel_path} ({lb_str} → {_LOUDNORM_TARGET_I:.0f} LUFS)")
        elif action == "skipped":
            skipped += 1
        elif action == "failed":
            failed += 1
            with job_lock:
                jobs[job_id]["log"].append(f"✗ Failed: {rel_path} — {evt.get('reason', 'unknown error')}")
        else:
            failed += 1
            with job_lock:
                jobs[job_id]["log"].append(f"✗ Failed: {rel_path} — {_extract_yt_dlp_error(stderr) or 'no result'}")

        with job_lock:
            jobs[job_id]["downloaded"] = normalized
            jobs[job_id]["failed"] = failed

    with job_lock:
        jobs[job_id]["log"].append(
            f"✅ Done: {normalized} normalized, {skipped} already within target/skipped, {failed} failed")

    if normalized and nd_cfg:
        with job_lock:
            jobs[job_id]["status"] = "scanning"
            jobs[job_id]["current_track"] = "Triggering Navidrome library scan…"
        ok, msg = nd_trigger_scan(nd_cfg)
        with job_lock:
            jobs[job_id]["log"].append(f"🔄 {msg}")
        if ok:
            nd_wait_for_scan(nd_cfg, timeout=300)

    with job_lock:
        jobs[job_id]["status"] = "done"
        jobs[job_id]["current_track"] = None
        save_jobs()

# ─── Sync health ────────────────────────────────────────────────────────────

def load_sync_health():
    try:
        with open(SYNC_HEALTH_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_sync_health(data):
    with open(SYNC_HEALTH_FILE, "w") as f:
        json.dump(data, f, indent=2)

def record_sync_health(playlist_id, downloaded, failed, total, status=None):
    if not playlist_id:
        return
    if status is None:
        status = "ok" if failed == 0 else ("partial" if downloaded > 0 else "failed")
    data = load_sync_health()
    data[playlist_id] = {
        "last_status": status, "downloaded": downloaded, "failed": failed,
        "total": total, "updated_at": datetime.utcnow().isoformat(),
    }
    save_sync_health(data)

# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/auth/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return jsonify({"error": "No code"}), 400
    auth = SpotifyOAuth(
        client_id=SPOTIFY_CLIENT_ID, client_secret=SPOTIFY_CLIENT_SECRET,
        redirect_uri=SPOTIFY_REDIRECT_URI,
        scope="playlist-read-private playlist-read-collaborative user-library-read",
        cache_path="/root/.ssh/.spotify_cache", open_browser=False)
    auth.get_access_token(code)
    return jsonify({"status": "ok"})

@app.route("/auth/status")
def auth_status():
    sp, url = get_sp()
    if sp:
        me = sp.me()
        return jsonify({"authenticated": True, "user": me["display_name"],
                        "image": me["images"][0]["url"] if me.get("images") else None})
    return jsonify({"authenticated": False, "auth_url": url})

@app.route("/navidrome/config", methods=["GET"])
def nd_get_config():
    cfg = load_nd_config()
    if cfg:
        return jsonify({"configured": True, "url": cfg["url"], "user": cfg["user"]})
    return jsonify({"configured": False})

@app.route("/navidrome/config", methods=["POST"])
def nd_set_config():
    data = request.json
    url, user, password = data.get("url","").strip(), data.get("user","").strip(), data.get("password","").strip()
    if not url or not user or not password:
        return jsonify({"error": "url, user and password required"}), 400
    try:
        nd_subsonic("ping", cfg={"url": url.rstrip("/"), "user": user, "password": password})
    except Exception as e:
        return jsonify({"error": f"Connection failed: {e}"}), 400
    save_nd_config(url, user, password)
    return jsonify({"status": "ok"})

@app.route("/ssh/config", methods=["GET"])
def ssh_get_config():
    cfg = load_ssh_config()
    pub = get_public_key()
    if cfg:
        return jsonify({"configured": True, "host": cfg["host"], "user": cfg["user"],
                        "port": cfg["port"], "music_path": cfg["music_path"], "public_key": pub})
    return jsonify({"configured": False, "public_key": pub})

@app.route("/ssh/config", methods=["POST"])
def ssh_set_config():
    data = request.json
    host = data.get("host","").strip()
    user = data.get("user","").strip()
    port = int(data.get("port", 22))
    music_path = data.get("music_path", "/opt/navidrome/music").strip()
    if not host or not user:
        return jsonify({"error": "host and user required"}), 400
    generate_ssh_key()
    cfg = {"host": host, "user": user, "port": port, "music_path": music_path}
    ok, err = test_ssh(cfg)
    if not ok:
        return jsonify({"error": f"SSH connection failed: {err}"}), 400
    save_ssh_config(host, user, port, music_path)
    return jsonify({"status": "ok"})

@app.route("/playlists")
def playlists():
    sp, url = get_sp()
    if not sp:
        return jsonify({"error": "Not authenticated", "auth_url": url}), 401
    results, offset = [], 0
    while True:
        batch = sp.current_user_playlists(limit=50, offset=offset)
        results.extend(batch["items"])
        if not batch["next"]: break
        offset += 50
    tracked = load_tracked()
    return jsonify([{"id": p["id"], "name": p["name"], "tracks": p["tracks"]["total"],
                     "image": p["images"][0]["url"] if p.get("images") else None,
                     "owner": p["owner"]["display_name"],
                     "tracked": p["id"] in tracked} for p in results])

@app.route("/playlist/<playlist_id>/tracks")
def playlist_tracks(playlist_id):
    sp, url = get_sp()
    if not sp:
        return jsonify({"error": "Not authenticated"}), 401
    return jsonify(fetch_playlist_tracks(sp, playlist_id))

@app.route("/tracked")
def get_tracked():
    return jsonify(load_tracked())

@app.route("/tracked/<playlist_id>", methods=["DELETE"])
def remove_tracked(playlist_id):
    data = load_tracked()
    data.pop(playlist_id, None)
    save_tracked(data)
    return jsonify({"status": "ok"})

@app.route("/download", methods=["POST"])
def start_download():
    data = request.json
    playlist_name = data.get("playlist_name", "Unknown Playlist")
    playlist_id   = data.get("playlist_id")
    tracks        = data.get("tracks", [])
    sync          = data.get("sync_navidrome", True)
    if not tracks:
        return jsonify({"error": "No tracks provided"}), 400
    job_id = f"job_{int(time.time()*1000)}"
    with job_lock:
        jobs[job_id] = {"id": job_id, "playlist": playlist_name, "status": "pending",
                        "total": len(tracks), "current": 0, "downloaded": 0, "failed": 0,
                        "nd_synced": None, "nd_missing": None, "current_track": None, "log": []}
    threading.Thread(target=download_worker,
                     args=(job_id, tracks, playlist_name, playlist_id, sync), daemon=True).start()
    return jsonify({"job_id": job_id})

@app.route("/jobs")
def list_jobs():
    with job_lock:
        save_jobs()
        return jsonify(list(jobs.values()))

@app.route("/jobs/<job_id>")
def get_job(job_id):
    with job_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    return jsonify(job)

@app.route("/jobs/<job_id>/skip", methods=["POST"])
def skip_track(job_id):
    with job_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    with job_lock:
        jobs[job_id]["skip_current"] = True
    return jsonify({"status": "ok"})

@app.route("/schedule", methods=["GET"])
def get_schedule():
    cfg = load_schedule_config()
    from datetime import timedelta
    now = datetime.utcnow()
    if cfg.get("enabled", True):
        secs = seconds_until_next_run(cfg)
        next_run = (now + timedelta(seconds=secs)).strftime("%Y-%m-%d %H:%M UTC")
    else:
        next_run = None
    return jsonify({**cfg, "next_run": next_run})

@app.route("/schedule", methods=["POST"])
def set_schedule():
    data = request.json
    cfg = {
        "enabled": bool(data.get("enabled", True)),
        "mode": data.get("mode", "time"),
        "hour": int(data.get("hour", 3)),
        "interval_hours": int(data.get("interval_hours", 24)),
    }
    save_schedule_config(cfg)
    return jsonify({"status": "ok", **cfg})

@app.route("/schedule/run", methods=["POST"])
def run_now():
    threading.Thread(target=auto_sync_worker, daemon=True).start()
    return jsonify({"status": "started"})

@app.route("/ytmusic/info", methods=["POST"])
def ytmusic_info():
    url = request.json.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    try:
        cmd = ["yt-dlp", "--dump-single-json", "--flat-playlist", url]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return jsonify({"error": result.stderr[-200:]}), 400
        info = json.loads(result.stdout)
        is_playlist = info.get("_type") in ("playlist", "multi_video")
        entries = info.get("entries", [info])
        return jsonify({
            "title": info.get("title", "Unknown"),
            "uploader": info.get("uploader") or info.get("channel", ""),
            "is_playlist": is_playlist,
            "track_count": len(entries),
            "thumbnail": info.get("thumbnail") or (entries[0].get("thumbnail") if entries else None),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/ytmusic/download", methods=["POST"])
def ytmusic_download():
    data = request.json
    url = data.get("url", "").strip()
    playlist_name = data.get("playlist_name", "YouTube Music")
    is_playlist = data.get("is_playlist", False)
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    job_id = f"yt_{int(time.time()*1000)}"
    with job_lock:
        jobs[job_id] = {"id": job_id, "playlist": f"[YT] {playlist_name}", "status": "pending",
                        "total": 0, "current": 0, "downloaded": 0, "failed": 0,
                        "nd_synced": None, "nd_missing": None, "current_track": None, "log": []}
    threading.Thread(target=ytmusic_download_worker,
                     args=(job_id, url, playlist_name, is_playlist), daemon=True).start()
    return jsonify({"job_id": job_id})

@app.route("/library/retag-genres", methods=["POST"])
def retag_genres():
    """Relabel genre tags across the whole existing library in place — scans
    and rewrites files directly on the Navidrome host over SSH, never
    re-downloading or otherwise touching anything but the genre tag."""
    ssh_cfg = load_ssh_config()
    if not ssh_cfg:
        return jsonify({"error": "SSH not configured"}), 400
    try:
        sp, url = get_sp()
    except Exception:
        sp, url = None, None
    if not sp:
        return jsonify({"error": "Spotify not authenticated", "auth_url": url}), 401

    job_id = f"genre_relabel_{int(time.time()*1000)}"
    with job_lock:
        jobs[job_id] = {"id": job_id, "playlist": "[Genre Relabel] Library", "status": "pending",
                        "total": 0, "current": 0, "downloaded": 0, "failed": 0,
                        "nd_synced": None, "nd_missing": None, "current_track": None, "log": []}
    threading.Thread(target=genre_relabel_worker, args=(job_id,), daemon=True).start()
    return jsonify({"job_id": job_id})

@app.route("/library/normalize-volume", methods=["POST"])
def normalize_volume():
    """Normalize loudness across the whole existing library in place —
    measures each file's integrated loudness on the Navidrome host over SSH
    and re-encodes (preserving every tag and embedded picture) only the
    files that fall outside the same target new downloads already use."""
    ssh_cfg = load_ssh_config()
    if not ssh_cfg:
        return jsonify({"error": "SSH not configured"}), 400

    job_id = f"volume_normalize_{int(time.time()*1000)}"
    with job_lock:
        jobs[job_id] = {"id": job_id, "playlist": "[Volume Normalize] Library", "status": "pending",
                        "total": 0, "current": 0, "downloaded": 0, "failed": 0,
                        "nd_synced": None, "nd_missing": None, "current_track": None, "log": []}
    threading.Thread(target=volume_normalize_worker, args=(job_id,), daemon=True).start()
    return jsonify({"job_id": job_id})

@app.route("/library/redownload-removed", methods=["POST"])
def redownload_removed():
    """One-off recovery for the too-aggressive dedupe sweep: un-ignores
    every track currently blocked because of it, and re-downloads whichever
    of them don't already have a plausible live copy somewhere in Navidrome
    under different tags (most of them do — it was usually just the entry
    that went missing, not the song itself). Safe to call again later; a
    clean ignore list just reports nothing to do."""
    ssh_cfg = load_ssh_config()
    nd_cfg = load_nd_config()
    if not ssh_cfg:
        return jsonify({"error": "SSH not configured"}), 400

    ignored = load_ignored_tracks()
    auto = [(k, v) for k, v in ignored.items() if str(v.get("reason", "")).startswith("Auto-removed")]
    if not auto:
        return jsonify({"queued": 0, "skipped": 0, "message": "Nothing to redownload"})

    to_fetch, skipped = [], []
    for i, (key, v) in enumerate(auto):
        artist, title = v["artist"], v["title"]
        if nd_cfg and _navidrome_has_close_match(title, artist, nd_cfg):
            skipped.append({"artist": artist, "title": title})
        else:
            to_fetch.append({"id": f"recovered_{i}", "name": title, "artist": artist,
                              "album": "", "album_artist": artist, "duration_ms": 0, "image": None})
        del ignored[key]
    save_ignored_tracks(ignored)

    if not to_fetch:
        return jsonify({"queued": 0, "skipped": len(skipped), "skipped_list": skipped,
                         "message": "Every removed track already has a live match in Navidrome — nothing to redownload."})

    job_id = f"recover_dedupe_{int(time.time()*1000)}"
    with job_lock:
        jobs[job_id] = {"id": job_id, "playlist": "[Recovered] Dedupe Undo", "status": "pending",
                        "total": 0, "current": 0, "downloaded": 0, "failed": 0,
                        "nd_synced": None, "nd_missing": None, "current_track": None, "log": []}
    threading.Thread(target=download_worker, args=(job_id, to_fetch, "Recovered Dedupe Undo", None, True),
                     daemon=True).start()
    return jsonify({"job_id": job_id, "queued": len(to_fetch), "skipped": len(skipped), "skipped_list": skipped})

@app.route("/library/orphans", methods=["GET"])
def library_orphans():
    """Read-only: list Navidrome library entries whose backing file no
    longer exists on disk (a "husk" left behind by a file deletion that
    wasn't followed by a full library scan)."""
    ssh_cfg = load_ssh_config()
    if not ssh_cfg:
        return jsonify({"error": "SSH not configured"}), 400
    try:
        orphans = find_orphaned_navidrome_entries(ssh_cfg)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"count": len(orphans), "entries": orphans})

@app.route("/library/orphans/prune", methods=["POST"])
def library_orphans_prune():
    """Deletes the Navidrome database rows found by /library/orphans (never
    touches disk — those files are already gone) and triggers a full scan
    to reconcile. Safe to call repeatedly; a clean library just reports 0."""
    ssh_cfg = load_ssh_config()
    if not ssh_cfg:
        return jsonify({"error": "SSH not configured"}), 400
    nd_cfg = load_nd_config()
    try:
        result = prune_orphaned_navidrome_entries(ssh_cfg, nd_cfg)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    if result.get("error"):
        return jsonify(result), 500
    return jsonify(result)


# ─── Cleanup routes ───────────────────────────────────────────────────────────

@app.route("/cleanup/scan", methods=["GET"])
def cleanup_scan():
    """Find duplicate tracks and tracks longer than 10 minutes in Navidrome."""
    cfg = load_nd_config()
    if not cfg:
        return jsonify({"error": "Navidrome not configured"}), 400

    ssh_cfg = load_ssh_config()

    try:
        # Get all songs via subsonic - paginate
        all_songs = []
        offset = 0
        while True:
            data = nd_subsonic("search3", cfg=cfg,
                               query="", songCount=500, songOffset=offset,
                               albumCount=0, artistCount=0)
            songs = data.get("searchResult3", {}).get("song", [])
            if not songs:
                break
            all_songs.extend(songs)
            if len(songs) < 500:
                break
            offset += 500

        # Find duplicates (same title + artist)
        seen = {}
        duplicates = []
        for s in all_songs:
            key = f"{s.get('title','').lower().strip()}|{s.get('artist','').lower().strip()}"
            if key in seen:
                # Keep the one with higher bitrate/size, mark other as duplicate
                existing = seen[key]
                existing_br = existing.get("bitRate", 0) or 0
                new_br = s.get("bitRate", 0) or 0
                if new_br >= existing_br:
                    duplicates.append({
                        "id": existing["id"],
                        "title": existing.get("title",""),
                        "artist": existing.get("artist",""),
                        "album": existing.get("album",""),
                        "duration": existing.get("duration", 0),
                        "bitRate": existing.get("bitRate", 0),
                        "path": existing.get("path",""),
                        "reason": "duplicate",
                        "kept_bitrate": new_br,
                    })
                    seen[key] = s
                else:
                    duplicates.append({
                        "id": s["id"],
                        "title": s.get("title",""),
                        "artist": s.get("artist",""),
                        "album": s.get("album",""),
                        "duration": s.get("duration", 0),
                        "bitRate": s.get("bitRate", 0),
                        "path": s.get("path",""),
                        "reason": "duplicate",
                        "kept_bitrate": existing_br,
                    })
            else:
                seen[key] = s

        # Find long tracks (> 10 minutes = 600 seconds)
        long_tracks = []
        for s in all_songs:
            duration = s.get("duration", 0) or 0
            if duration > 600:
                long_tracks.append({
                    "id": s["id"],
                    "title": s.get("title",""),
                    "artist": s.get("artist",""),
                    "album": s.get("album",""),
                    "duration": duration,
                    "bitRate": s.get("bitRate", 0),
                    "path": s.get("path",""),
                    "reason": "long",
                })

        return jsonify({
            "total_scanned": len(all_songs),
            "duplicates": duplicates,
            "long_tracks": long_tracks,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/cleanup/delete", methods=["POST"])
def cleanup_delete():
    """Delete tracks from Navidrome and optionally from disk.

    2026-08-26: this used to resolve each file's path via the Subsonic
    getSong API's own 'path' field — which turned out to sometimes be a
    virtual/display path (artist/album/title-derived) rather than the
    literal filesystem path Navidrome actually indexed the file at (the
    exact same lesson learned the hard way with a permission-denied bug
    around the same time — see find_orphaned_navidrome_entries). Worse,
    the delete used `rm -f`, which exits 0 even when the target doesn't
    exist — so a wrong path meant the real file was silently never
    touched while the response still reported success. Now reads the
    real path straight from the media_file table (one batched SQL
    query, like find_orphaned_navidrome_entries does) instead of
    trusting the API's convenience field."""
    data = request.json
    track_ids = data.get("track_ids", [])
    delete_files = data.get("delete_files", True)

    if not track_ids:
        return jsonify({"error": "No track IDs provided"}), 400

    cfg = load_nd_config()
    ssh_cfg = load_ssh_config()

    if not cfg:
        return jsonify({"error": "Navidrome not configured"}), 400

    deleted = []
    failed = []

    if delete_files and ssh_cfg:
        ids_str = ",".join(f"'{tid}'" for tid in track_ids)
        select_cmd = _ssh_cmd(ssh_cfg,
            "sqlite3 /var/lib/navidrome/navidrome.db "
            f"\"SELECT id || char(1) || path FROM media_file WHERE id IN ({ids_str});\"")
        result = subprocess.run(select_cmd, capture_output=True, text=True, timeout=30)
        real_paths = {}
        for line in result.stdout.splitlines():
            if "\x01" not in line:
                continue
            tid, path = line.split("\x01", 1)
            real_paths[tid] = path
        print(f"[cleanup] real paths from DB: {real_paths}", file=sys.stderr)

        for tid in track_ids:
            path = real_paths.get(tid)
            if not path:
                failed.append(f"{tid}: no path found in Navidrome's DB")
                continue
            full_path = path if path.startswith("/") else f"{ssh_cfg['music_path']}/{path}"
            # Confirm the file actually exists before claiming success —
            # rm -f exits 0 either way, which is exactly what let this
            # silently no-op before.
            check_cmd = _ssh_cmd(ssh_cfg, f"test -f {shlex.quote(full_path)} && echo yes || echo no")
            exists = subprocess.run(check_cmd, capture_output=True, text=True, timeout=10).stdout.strip() == "yes"
            if not exists:
                failed.append(f"{path}: file not found at {full_path}")
                continue
            rm_cmd = _ssh_cmd(ssh_cfg, f"rm -f -- {shlex.quote(full_path)}")
            result = subprocess.run(rm_cmd, capture_output=True, text=True, timeout=15)
            print(f"[cleanup] rm '{full_path}' -> rc={result.returncode} err={result.stderr}", file=sys.stderr)
            if result.returncode == 0:
                deleted.append(path)
            else:
                failed.append(f"{path}: {result.stderr}")

    print(f"[cleanup] Deleting {len(track_ids)} tracks, delete_files={delete_files}", file=sys.stderr)

    # Delete directly from Navidrome SQLite DB via SSH
    if track_ids and ssh_cfg:
        ids_str = ",".join(f"'{tid}'" for tid in track_ids)
        sql = f"DELETE FROM media_file WHERE id IN ({ids_str});"
        cmd = _ssh_cmd(ssh_cfg, f"sqlite3 /var/lib/navidrome/navidrome.db {shlex.quote(sql)}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        print(f"[cleanup] DB delete rc={result.returncode} err={result.stderr.strip()}", file=sys.stderr)

    # Also trigger a full scan to clean up
    ok, msg = nd_trigger_scan(cfg, full=True)
    if ok:
        nd_wait_for_scan(cfg, timeout=300)

    return jsonify({
        "deleted_files": len(deleted),
        "failed": len(failed),
        "failed_list": failed[:10],
    })



# ─── yt-dlp management ───────────────────────────────────────────────────────

@app.route("/ytdlp/version")
def ytdlp_version():
    try:
        result = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True, timeout=10)
        return jsonify({"version": result.stdout.strip()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/ytdlp/update", methods=["POST"])
def ytdlp_update():
    try:
        result = subprocess.run(
            ["yt-dlp", "-U"],
            capture_output=True, text=True, timeout=60
        )
        output = result.stdout + result.stderr
        return jsonify({"status": "ok", "output": output.strip()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─── Cookies management ───────────────────────────────────────────────────────

COOKIES_FILE = "/root/.ssh/yt_cookies.txt"

@app.route("/cookies/status")
def cookies_status():
    exists = os.path.exists(COOKIES_FILE)
    if exists:
        size = os.path.getsize(COOKIES_FILE)
        mtime = os.path.getmtime(COOKIES_FILE)
        from datetime import datetime
        modified = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        return jsonify({"configured": True, "size": size, "modified": modified})
    return jsonify({"configured": False})

@app.route("/cookies/upload", methods=["POST"])
def cookies_upload():
    data = request.json
    content_str = data.get("content", "").strip()
    if not content_str:
        return jsonify({"error": "No cookie content provided"}), 400
    if "youtube" not in content_str.lower() and "HTTP Cookie" not in content_str and "# Netscape" not in content_str:
        return jsonify({"error": "Doesn\'t look like a valid Netscape cookie file"}), 400
    with open(COOKIES_FILE, "w") as f:
        f.write(content_str)
    return jsonify({"status": "ok"})

@app.route("/cookies/delete", methods=["DELETE"])
def cookies_delete():
    try:
        if os.path.exists(COOKIES_FILE):
            os.remove(COOKIES_FILE)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─── Ignored tracks ─────────────────────────────────────────────────────────

@app.route("/tracked/ignored", methods=["GET"])
def list_ignored_tracks():
    return jsonify(load_ignored_tracks())

@app.route("/tracked/ignored", methods=["POST"])
def add_ignored_track():
    data = request.json or {}
    artist, title = data.get("artist", ""), data.get("title", "")
    if not artist or not title:
        return jsonify({"error": "artist and title required"}), 400
    key = track_ignore_key(artist, title)
    ignored = load_ignored_tracks()
    ignored[key] = {"artist": artist, "title": title, "added_at": datetime.utcnow().isoformat()}
    save_ignored_tracks(ignored)
    return jsonify({"status": "ok", "key": key})

@app.route("/tracked/ignored", methods=["DELETE"])
def remove_ignored_track():
    data = request.json or {}
    artist, title = data.get("artist", ""), data.get("title", "")
    if not artist or not title:
        return jsonify({"error": "artist and title required"}), 400
    key = track_ignore_key(artist, title)
    ignored = load_ignored_tracks()
    ignored.pop(key, None)
    save_ignored_tracks(ignored)
    return jsonify({"status": "ok"})

# ─── Failed downloads (manual-retry) ───────────────────────────────────────────

@app.route("/failed")
def list_failed_tracks_route():
    data = load_failed_tracks()
    result = [{**entry, "key": key} for key, entry in data.items()
              if not is_track_ignored(entry.get("artist"), entry.get("name"))]
    result.sort(key=lambda e: e.get("last_attempt") or "", reverse=True)
    return jsonify(result)

@app.route("/failed/dismiss", methods=["POST"])
def dismiss_failed_track():
    key = (request.json or {}).get("key", "")
    if not key:
        return jsonify({"error": "key required"}), 400
    with failed_tracks_lock:
        data = load_failed_tracks()
        data.pop(key, None)
        save_failed_tracks(data)
    return jsonify({"status": "ok"})

@app.route("/failed/retry", methods=["POST"])
def retry_failed_track():
    body = request.json or {}
    key = body.get("key", "")
    url = (body.get("url") or "").strip()
    if not key or not url:
        return jsonify({"error": "key and url required"}), 400

    entry = load_failed_tracks().get(key)
    if not entry:
        return jsonify({"error": "Unknown failed track (it may already be resolved)"}), 404

    artist, title = entry.get("artist", ""), entry.get("name", "")
    album = entry.get("album") or "Unknown Album"
    album_artist = entry.get("album_artist")
    playlist_name = entry.get("playlist_name") or "Manual Downloads"
    ssh_cfg = load_ssh_config()
    nd_cfg = load_nd_config()

    local_dir = os.path.join(DOWNLOAD_DIR, sanitize(playlist_name))
    album_dir = os.path.join(local_dir, sanitize(album))
    os.makedirs(album_dir, exist_ok=True)
    filename = sanitize(f"{artist} - {title}")
    out_template = os.path.join(album_dir, f"{filename}.%(ext)s")

    tmp_job_id = f"manual_retry_{int(time.time()*1000)}"
    with job_lock:
        jobs[tmp_job_id] = {"id": tmp_job_id, "playlist": f"[Manual retry] {playlist_name}",
                            "status": "running", "total": 1, "current": 1,
                            "downloaded": 0, "failed": 0, "nd_synced": None, "nd_missing": None,
                            "current_track": f"{artist} - {title}", "log": []}

    is_youtube_url = "youtube.com" in url or "youtu.be" in url
    use_cookies = is_youtube_url and os.path.exists(COOKIES_FILE)
    rc, killed, stdout, stderr = _download_via_yt_dlp(
        url, out_template, tmp_job_id, f"{artist} - {title}", use_cookies=use_cookies)

    if use_cookies and killed is None and rc != 0 and "403" in (stderr or ""):
        with job_lock:
            jobs[tmp_job_id]["log"].append("↻ Retrying without cookies after 403")
        rc, killed, stdout, stderr = _download_via_yt_dlp(
            url, out_template, tmp_job_id, f"{artist} - {title}", use_cookies=False)

    used_android_fallback = False
    if is_youtube_url and killed is None and rc != 0 and "403" in (stderr or ""):
        used_android_fallback = True
        with job_lock:
            jobs[tmp_job_id]["log"].append("↻ Retrying via android client (lower quality) after repeated 403")
        rc, killed, stdout, stderr = _download_via_yt_dlp(
            url, out_template, tmp_job_id, f"{artist} - {title}", use_cookies=False, player_client="android")

    with job_lock:
        jobs[tmp_job_id]["status"] = "done"

    if killed == "timeout":
        return jsonify({"success": False, "message": "Download timed out after 30s"})
    if killed == "skipped":
        return jsonify({"success": False, "message": "Download was skipped"})
    if rc != 0:
        reason = _extract_yt_dlp_error(stderr) or f"yt-dlp exited with code {rc}"
        return jsonify({"success": False, "message": reason})

    flac_path = out_template.replace(".%(ext)s", ".flac")
    if not os.path.exists(flac_path):
        return jsonify({"success": False, "message": "File missing after download"})

    source_url = extract_resolved_url(stdout) or url
    genre = lookup_genre(artist)
    fix_tags(flac_path, title, artist, album, album_artist=album_artist,
             source_url=source_url, genre=genre)
    new_album, flac_path = maybe_correct_album(
        flac_path, title, artist, album, playlist_name, source_url, local_dir, album_artist=album_artist)

    synced_to_navidrome = False
    if ssh_cfg:
        retried_track = {"id": key, "name": title, "artist": artist, "album": new_album,
                          "album_artist": album_artist, "duration_ms": entry.get("duration_ms", 0),
                          "image": None, "source_url": source_url}
        batch_upload_and_cleanup(local_dir, ssh_cfg, nd_cfg, playlist_name, [retried_track], tmp_job_id)
        synced_to_navidrome = True

    clear_failed_track(artist, title)
    with job_lock:
        jobs[tmp_job_id]["downloaded"] = 1

    message = f"Downloaded (album: {new_album})"
    message += " and synced to Navidrome" if synced_to_navidrome else " — no SSH configured, file left in local storage"
    if used_android_fallback:
        message += " — ⚠ lower quality (android fallback after repeated YouTube 403s)"
    return jsonify({"success": True, "message": message})

# ─── Sync health routes ────────────────────────────────────────────────────────

@app.route("/health/dead-links")
def health_dead_links():
    return jsonify(load_dead_links())

@app.route("/health/duplicate-report")
def health_duplicate_report():
    return jsonify(load_duplicate_report())

@app.route("/health/summary")
def health_summary():
    tracked = load_tracked()
    sync_health = load_sync_health()
    dead_links = load_dead_links()

    playlists = []
    for playlist_id, info in tracked.items():
        health = sync_health.get(playlist_id, {})
        playlists.append({
            "id": playlist_id,
            "name": info.get("name"),
            "last_synced": info.get("last_synced"),
            "last_status": health.get("last_status", "unknown"),
            "downloaded": health.get("downloaded"),
            "failed": health.get("failed"),
            "dead_link_count": len(dead_links.get(playlist_id, [])),
        })

    unknown_album_count = None
    nd_cfg = load_nd_config()
    if nd_cfg:
        try:
            data = nd_subsonic("search3", cfg=nd_cfg, query="Unknown Album",
                               songCount=500, albumCount=0, artistCount=0)
            songs = data.get("searchResult3", {}).get("song", [])
            unknown_album_count = sum(1 for s in songs if s.get("album", "").strip().lower() == "unknown album")
        except Exception:
            unknown_album_count = None

    return jsonify({
        "playlists": playlists,
        "unknown_album_count": unknown_album_count,
        "duplicate_report": load_duplicate_report(),
        "title_duplicate_report": load_title_duplicate_report(),
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
