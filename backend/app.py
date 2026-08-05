import os, json, threading, time, re, subprocess, shutil, signal, sys, shlex, difflib
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import requests as http
from flask import Flask, jsonify, request
from flask_cors import CORS
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, COMM, error as ID3Error
from ytmusicapi import YTMusic

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR          = "/downloads"
YTDLP_POT_ARGS        = ["--extractor-args", "youtubepot-bgutilhttp:base_url=http://bgutil-pot:4416",
                          "--extractor-args", "youtube:player_client=mweb",
                          "--remote-components", "ejs:github"]
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
SYNC_HEALTH_FILE      = "/root/.ssh/sync_health.json"

jobs = {}
job_lock = threading.Lock()
nd_playlist_lock = threading.Lock()

def save_jobs():
    try:
        with open(JOBS_FILE, "w") as f:
            json.dump(jobs, f)
    except Exception:
        pass

def load_jobs():
    global jobs
    try:
        with open(JOBS_FILE) as f:
            jobs = json.load(f)
    except Exception:
        jobs = {}

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
        token = auth.refresh_access_token(token["refresh_token"])
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

def fix_tags(filepath, title, artist, album, album_artist=None, source_url=None):
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

def _get_ytmusic():
    """Lazily construct a shared YTMusic client. If construction ever fails
    (e.g. no network at startup), disable it for the rest of the process
    instead of retrying on every single track."""
    global _ytmusic_client, _ytmusic_disabled
    if _ytmusic_disabled:
        return None
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

def find_best_track_source(track):
    """Try, in order: YouTube Music songs, YouTube Music videos, plain
    YouTube, SoundCloud. Returns (url, matched_title, provider_label) for the
    first confident match, or (None, None, None) if nothing qualifies on any
    provider. Each provider is scored independently on title similarity,
    artist match, and duration closeness — no provider's result #1 is ever
    taken blindly."""
    expected_title = track.get("name") or ""
    expected_artist = track.get("artist") or ""
    expected_duration_sec = (track.get("duration_ms") or 0) / 1000
    artist_for_query = primary_artist(expected_artist)
    query = f"{artist_for_query} {expected_title}".strip()

    best = _search_ytmusic(query, "songs", expected_title, expected_artist, expected_duration_sec)
    if best:
        return f"https://music.youtube.com/watch?v={best['videoId']}", best.get("title"), "YouTube Music"

    best = _search_ytmusic(query, "videos", expected_title, expected_artist, expected_duration_sec)
    if best:
        return f"https://music.youtube.com/watch?v={best['videoId']}", best.get("title"), "YouTube Music (video)"

    best = _search_yt_dlp(f"ytsearch8:{query} audio", expected_title, expected_artist, expected_duration_sec)
    if best:
        return best["url"], best["title"], "YouTube"

    best = _search_yt_dlp(f"scsearch8:{query}", expected_title, expected_artist, expected_duration_sec)
    if best:
        return best["url"], best["title"], "SoundCloud"

    return None, None, None

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

        video_url, _matched_title, provider = find_best_track_source(track)
        if not video_url:
            with job_lock:
                jobs[job_id]["log"].append(
                    f"✗ No confident match found (tried YouTube Music, YouTube, SoundCloud): "
                    f"{track['artist']} - {track['name']}")
                jobs[job_id]["failed"] += 1
            continue

        is_youtube_url = "youtube.com" in video_url or "youtu.be" in video_url
        pot_args = YTDLP_POT_ARGS if is_youtube_url else []
        cookies_args = ["--cookies", COOKIES_FILE] if (is_youtube_url and os.path.exists(COOKIES_FILE)) else []
        cmd = ["yt-dlp",
               "-x", "--audio-format", "flac", "--audio-quality", "0",
               "--add-metadata", "--embed-thumbnail", "--output", out_template,
               "--no-playlist",
               "--retries", "1", "--fragment-retries", "1", "--extractor-retries", "1",
               "--concurrent-fragments", "1", "--socket-timeout", "10",
               "--sleep-interval", "2", "--max-sleep-interval", "4",
               "--no-progress", "--print", "before_dl:%(webpage_url)s",
               ] + pot_args + cookies_args + [video_url]

        rc, killed, stdout, stderr = run_yt_dlp(cmd, job_id, f"{track['artist']} - {track['name']}", timeout=30)

        if killed == "skipped":
            with job_lock:
                jobs[job_id]["log"].append(f"⏭ Manually skipped: {track['artist']} - {track['name']}")
                jobs[job_id]["failed"] += 1
        elif killed == "timeout":
            with job_lock:
                jobs[job_id]["log"].append(f"✗ Timeout (30s, via {provider}): {track['artist']} - {track['name']}")
                jobs[job_id]["failed"] += 1
        elif rc == 0:
            flac_path = out_template.replace('.%(ext)s', '.flac')
            if not os.path.exists(flac_path):
                with job_lock:
                    jobs[job_id]["log"].append(f"✗ Failed (file missing after download): {track['artist']} - {track['name']}")
                    jobs[job_id]["failed"] += 1
                continue
            source_url = extract_resolved_url(stdout)
            track["source_url"] = source_url
            fix_tags(flac_path, track['name'], track['artist'], track['album'],
                     album_artist=track.get('album_artist'), source_url=source_url)
            new_album, flac_path = maybe_correct_album(
                flac_path, track['name'], track['artist'], track['album'],
                playlist_name, source_url, local_dir, album_artist=track.get('album_artist'))
            if new_album != track['album']:
                with job_lock:
                    jobs[job_id]["log"].append(f"🏷 Corrected album: {track['album']} → {new_album}")
                track['album'] = new_album
            newly_downloaded.append(track)
            all_synced_tracks.append(track)
            with job_lock:
                jobs[job_id]["log"].append(f"✓ Downloaded via {provider}: {track['artist']} - {track['name']}")
                jobs[job_id]["downloaded"] += 1
            if len(newly_downloaded) % 50 == 0 and ssh_cfg:
                batch_upload_and_cleanup(local_dir, ssh_cfg, nd_cfg, playlist_name, list(all_synced_tracks), job_id)
                newly_downloaded.clear()
        else:
            reason = _extract_yt_dlp_error(stderr) or f"yt-dlp exited with code {rc}"
            with job_lock:
                jobs[job_id]["log"].append(f"✗ Failed ({reason}, via {provider}): {track['artist']} - {track['name']}")
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

        cookies_args = ["--cookies", COOKIES_FILE] if os.path.exists(COOKIES_FILE) else []
        cmd = ["yt-dlp",
               "-x", "--audio-format", "flac", "--audio-quality", "0",
               "--add-metadata", "--embed-thumbnail",
               "--output", out_template,
               "--no-playlist" if not is_playlist else "--yes-playlist",
               "--retries", "1", "--fragment-retries", "1", "--extractor-retries", "1",
               "--concurrent-fragments", "1", "--socket-timeout", "10",
               "--sleep-interval", "2", "--max-sleep-interval", "4",
               "--no-progress",
               ] + YTDLP_POT_ARGS + cookies_args + [track_url]

        rc, killed, _stdout, stderr = run_yt_dlp(cmd, job_id, f"{artist} - {title}", timeout=30)

        if killed == "skipped":
            with job_lock:
                jobs[job_id]["log"].append(f"⏭ Manually skipped: {artist} - {title}")
                jobs[job_id]["failed"] += 1
        elif killed == "timeout":
            with job_lock:
                jobs[job_id]["log"].append(f"✗ Timeout (30s): {artist} - {title}")
                jobs[job_id]["failed"] += 1
        elif rc == 0:
            flac_path = out_template.replace(".%(ext)s", ".flac")
            if not os.path.exists(flac_path):
                with job_lock:
                    jobs[job_id]["log"].append(f"✗ Failed (file missing after download): {artist} - {title}")
                    jobs[job_id]["failed"] += 1
                continue
            fix_tags(flac_path, title, artist, album, source_url=track_url)
            new_album, flac_path = maybe_correct_album(
                flac_path, title, artist, album, playlist_name, track_url, local_dir)
            if new_album != album:
                with job_lock:
                    jobs[job_id]["log"].append(f"🏷 Corrected album: {album} → {new_album}")
                album = new_album
                t["album"] = new_album
            downloaded_tracks.append(t)
            yt_track_list.append(t)
            with job_lock:
                jobs[job_id]["log"].append(f"✓ Downloaded: {artist} - {title}")
                jobs[job_id]["downloaded"] += 1
            if len(downloaded_tracks) % 50 == 0 and ssh_cfg:
                batch_upload_and_cleanup(local_dir, ssh_cfg, nd_cfg, playlist_name, list(downloaded_tracks), job_id)
        else:
            reason = _extract_yt_dlp_error(stderr) or f"yt-dlp exited with code {rc}"
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
        print("[auto-sync] No tracked playlists, skipping.")
        return
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
        nd_trigger_scan(nd_cfg)

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
    """Delete tracks from Navidrome and optionally from disk."""
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

    # Get file paths before deleting from Navidrome
    paths_to_delete = []
    if delete_files and ssh_cfg:
        for tid in track_ids:
            try:
                data_song = nd_subsonic("getSong", cfg=cfg, id=tid)
                song = data_song.get("song", {})
                path = song.get("path", "")
                print(f"[cleanup] track {tid} path='{path}'", file=sys.stderr)
                if path:
                    paths_to_delete.append(path)
            except Exception as e:
                print(f"[cleanup] getSong failed for {tid}: {e}", file=sys.stderr)
    print(f"[cleanup] paths_to_delete={paths_to_delete}", file=sys.stderr)

    # Delete from Navidrome by removing from all playlists and marking as deleted
    # Use subsonic star/unstar doesn't delete - we need to delete the file
    # Delete files via SSH
    if delete_files and ssh_cfg and paths_to_delete:
        for path in paths_to_delete:
            try:
                # Path from Navidrome is relative to music folder
                full_path = path if path.startswith("/") else f"{ssh_cfg['music_path']}/{path}"
                # Use a safer deletion approach that handles special chars in filenames
                cmd = ["ssh", "-i", "/root/.ssh/id_rsa",
                       "-p", str(ssh_cfg["port"]),
                       "-o", "StrictHostKeyChecking=no",
                       "-o", "BatchMode=yes",
                       f"{ssh_cfg['user']}@{ssh_cfg['host']}",
                       f"rm -f -- {repr(full_path)}"]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
                print(f"[cleanup] rm '{full_path}' -> rc={result.returncode} err={result.stderr}", file=sys.stderr)
                if result.returncode == 0:
                    deleted.append(path)
                    # Create empty placeholder so Navidrome detects folder still exists
                    # This ensures scanner marks the track as missing
                    folder = os.path.dirname(full_path)
                    placeholder_cmd = ["ssh", "-i", "/root/.ssh/id_rsa",
                                      "-p", str(ssh_cfg["port"]),
                                      "-o", "StrictHostKeyChecking=no",
                                      "-o", "BatchMode=yes",
                                      f"{ssh_cfg['user']}@{ssh_cfg['host']}",
                                      f"mkdir -p {repr(folder)}"]
                    subprocess.run(placeholder_cmd, capture_output=True, timeout=10)
                else:
                    failed.append(f"{path}: {result.stderr}")
            except Exception as e:
                failed.append(f"{path}: {e}")

    print(f"[cleanup] Deleting {len(track_ids)} tracks, delete_files={delete_files}", file=sys.stderr)
    # Debug: check what getSong returns
    if track_ids:
        try:
            test = nd_subsonic("getSong", cfg=cfg, id=track_ids[0])
            print(f"[cleanup] getSong result: {test}", file=sys.stderr)
        except Exception as e:
            print(f"[cleanup] getSong error: {e}", file=sys.stderr)

    # Get all song info in one batch via search, collect any remaining paths
    if track_ids and ssh_cfg:
        # Fetch all song info at once using the scan data already in memory
        # Use the songs from the scan result passed via request
        songs_info = request.json.get("songs_info", {})
        for tid in track_ids:
            song = songs_info.get(tid, {})
            if not song:
                try:
                    data_song = nd_subsonic("getSong", cfg=cfg, id=tid)
                    song = data_song.get("song", {})
                except Exception:
                    pass
            path = song.get("path","")
            if path:
                paths_to_delete.append(path)

    # Delete directly from Navidrome SQLite DB via SSH
    if track_ids and ssh_cfg:
        ids_str = ",".join(f"\'{tid}\'" for tid in track_ids)
        sql = f"DELETE FROM media_file WHERE id IN ({ids_str});"
        cmd = ["ssh", "-i", "/root/.ssh/id_rsa",
               "-p", str(ssh_cfg["port"]),
               "-o", "StrictHostKeyChecking=no",
               "-o", "BatchMode=yes",
               f"{ssh_cfg['user']}@{ssh_cfg['host']}",
               f"sqlite3 /var/lib/navidrome/navidrome.db {repr(sql)}"]
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
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
