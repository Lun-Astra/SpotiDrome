import os, json, threading, time, re, subprocess, shutil, signal, sys, shlex, difflib, uuid, base64, hashlib, secrets
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FutureTimeoutError
import requests as http
from flask import Flask, jsonify, request, Response
from flask_cors import CORS
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TCON, COMM, TRCK, TPOS, error as ID3Error
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
SOURCE_NAVIDROME_CONFIG_FILE = "/root/.ssh/source_navidrome_config.json"  # a *different* person's Navidrome, browsed as a download source
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
LONG_TRACK_WHITELIST_FILE = "/root/.ssh/long_track_whitelist.json"
LONG_TRACK_THRESHOLD_SEC  = 15 * 60  # 15 minutes

# ─── Access control (web login sessions + API keys) ────────────────────────
# SpotiDrome is reachable from the internet, so every route is deny-by-default:
# a request needs either a web session (cookie, from logging in with a
# Navidrome *admin* account) or an API key (for apps like LunaDrome). Only
# the login endpoint and the "who am I" check are public. Keys and session
# tokens are stored as SHA-256 hashes, never in the clear.
SESSIONS_FILE    = "/root/.ssh/web_sessions.json"
API_KEYS_FILE    = "/root/.ssh/api_keys.json"
SESSION_COOKIE   = "sd_session"
SESSION_TTL_SEC  = 30 * 24 * 3600
LAST_SEEN_WRITE_SEC = 300   # don't rewrite the store on every single request

# What an API key may call, per scope. "lunadrome" is what LunaDrome needs:
# search, album lookup, YT Music downloads and following its jobs - it can't
# change settings, delete tracks or mint keys. "full" is everything the web
# UI can do except managing API keys (that always needs a real login).
API_KEY_SCOPES = {
    "lunadrome": {
        ("GET", "/session"),
        ("GET", "/search"),
        ("GET", "/search/album/<browse_id>"),
        ("POST", "/ytmusic/info"),
        ("POST", "/ytmusic/download"),
        ("GET", "/jobs"),
        ("GET", "/jobs/<job_id>"),
        ("POST", "/jobs/<job_id>/skip"),
        ("POST", "/jobs/<job_id>/cancel"),
        ("GET", "/ytdlp/version"),
    },
    "full": None,  # None = every route
}
PUBLIC_ROUTES = {("POST", "/session/login"), ("GET", "/session"), ("POST", "/session/logout")}
SESSION_ONLY_ROUTES = {("GET", "/api-keys"), ("POST", "/api-keys"), ("DELETE", "/api-keys/<key_id>")}

# Password guessing is limited globally (not per client, so it can't be
# dodged by spreading guesses over many IPs): 10 wrong logins within 10
# minutes lock password login for 10 minutes. Existing sessions and API keys
# keep working during a lockout.
LOGIN_MAX_FAILURES = 10
LOGIN_FAILURE_WINDOW_SEC = 600
LOGIN_LOCKOUT_SEC = 600

_auth_lock = threading.Lock()
_login_failures = []
_login_locked_until = 0.0


def _hash_secret(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def _load_auth_store(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_auth_store(path, data):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def _request_is_https():
    return request.is_secure or request.headers.get("X-Forwarded-Proto", "").lower() == "https"


def _session_from_request():
    """Returns (token_hash, session dict) for a live session cookie, else (None, None)."""
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        return None, None
    h = _hash_secret(token)
    now = time.time()
    with _auth_lock:
        sessions = _load_auth_store(SESSIONS_FILE)
        s = sessions.get(h)
        if not s or s.get("expires_at", 0) <= now:
            return None, None
        if now - s.get("last_seen_at", 0) > LAST_SEEN_WRITE_SEC:
            s["last_seen_at"] = now
            _save_auth_store(SESSIONS_FILE, sessions)
    return h, s


def _api_key_from_request():
    """Returns the key record for a valid API key header, else None."""
    auth = request.headers.get("Authorization", "")
    key = auth[7:].strip() if auth[:7].lower() == "bearer " else request.headers.get("X-API-Key", "").strip()
    if not key:
        return None
    h = _hash_secret(key)
    now = time.time()
    with _auth_lock:
        keys = _load_auth_store(API_KEYS_FILE)
        for k in keys.values():
            if secrets.compare_digest(k.get("hash", ""), h):
                if now - (k.get("last_used_at") or 0) > LAST_SEEN_WRITE_SEC:
                    k["last_used_at"] = now
                    _save_auth_store(API_KEYS_FILE, keys)
                return k
    return None


def _identify():
    """Who is making this request: a dict describing the session or key, or None."""
    _h, s = _session_from_request()
    if s:
        return {"via": "session", "user": s["user"], "scope": "full"}
    k = _api_key_from_request()
    if k:
        return {"via": "api_key", "key_id": k["id"], "key_name": k["name"], "scope": k["scope"]}
    return None


@app.before_request
def _require_auth():
    if request.method == "OPTIONS":   # CORS preflight carries no credentials
        return None
    rule = request.url_rule.rule if request.url_rule else None
    if rule is None:                  # unknown URL - let Flask answer 404
        return None
    route = (request.method, rule)
    if route in PUBLIC_ROUTES:
        return None
    who = _identify()
    if not who:
        return jsonify({"error": "Login required", "login_required": True}), 401
    # A logged-in browser sends its cookie on any request to this site - also
    # ones another page (e.g. a sibling subdomain of the same domain, which SameSite
    # treats as the same site) makes it send. So a change made with the cookie
    # must also carry the header our own pages add (auth.js); another origin
    # can't add a custom header without a CORS preflight, which fails for
    # credentialed requests here.
    if who["via"] == "session" and request.method not in ("GET", "HEAD") \
            and request.headers.get("X-SD-Web") != "1":
        return jsonify({"error": "Missing X-SD-Web header"}), 403
    if route in SESSION_ONLY_ROUTES and who["via"] != "session":
        return jsonify({"error": "Managing API keys needs a web login, not an API key"}), 403
    allowed = API_KEY_SCOPES.get(who["scope"])
    if who["via"] == "api_key" and allowed is not None and route not in allowed:
        return jsonify({"error": f"This API key's scope ({who['scope']}) doesn't allow "
                                 f"{request.method} {rule}"}), 403
    return None


def _navidrome_admin_login(username, password):
    """Checks the credentials against the destination Navidrome's own login.
    Returns (ok, error). Only Navidrome admins may log in to SpotiDrome."""
    cfg = load_nd_config()
    if not cfg:
        return False, "Navidrome isn't configured on the server"
    try:
        r = http.post(f"{cfg['url']}/auth/login", json={"username": username, "password": password}, timeout=15)
    except Exception:
        return False, "Couldn't reach Navidrome to check the login"
    if r.status_code != 200:
        return False, "Wrong username or password"
    if not r.json().get("isAdmin"):
        return False, "Only Navidrome admins can use SpotiDrome"
    return True, None


@app.route("/session", methods=["GET"])
def session_get():
    who = _identify()
    if not who:
        return jsonify({"logged_in": False})
    return jsonify({"logged_in": True, **who})


@app.route("/session/login", methods=["POST"])
def session_login():
    global _login_locked_until
    data = request.json or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        return jsonify({"error": "Username and password required"}), 400
    with _auth_lock:
        if time.time() < _login_locked_until:
            return jsonify({"error": "Too many failed logins - try again in a few minutes"}), 429
    ok, err = _navidrome_admin_login(username, password)
    if not ok:
        now = time.time()
        with _auth_lock:
            _login_failures[:] = [t for t in _login_failures if now - t < LOGIN_FAILURE_WINDOW_SEC] + [now]
            if len(_login_failures) >= LOGIN_MAX_FAILURES:
                _login_locked_until = now + LOGIN_LOCKOUT_SEC
                _login_failures.clear()
                print(f"[auth] {LOGIN_MAX_FAILURES} failed logins in {LOGIN_FAILURE_WINDOW_SEC}s - "
                      f"password login locked for {LOGIN_LOCKOUT_SEC}s", file=sys.stderr, flush=True)
        time.sleep(1)
        return jsonify({"error": err}), 403
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _auth_lock:
        sessions = _load_auth_store(SESSIONS_FILE)
        sessions = {h: s for h, s in sessions.items() if s.get("expires_at", 0) > now}
        sessions[_hash_secret(token)] = {"user": username, "created_at": now, "last_seen_at": now,
                                         "expires_at": now + SESSION_TTL_SEC}
        _save_auth_store(SESSIONS_FILE, sessions)
    resp = jsonify({"logged_in": True, "user": username})
    resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL_SEC, httponly=True,
                    secure=_request_is_https(), samesite="Lax", path="/")
    return resp


@app.route("/session/logout", methods=["POST"])
def session_logout():
    h, _s = _session_from_request()
    if h:
        with _auth_lock:
            sessions = _load_auth_store(SESSIONS_FILE)
            sessions.pop(h, None)
            _save_auth_store(SESSIONS_FILE, sessions)
    resp = jsonify({"logged_in": False})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


def _public_key_record(k):
    return {f: k.get(f) for f in ("id", "name", "scope", "prefix", "created_at", "created_by", "last_used_at")}


@app.route("/api-keys", methods=["GET"])
def api_keys_list():
    keys = _load_auth_store(API_KEYS_FILE)
    return jsonify(sorted((_public_key_record(k) for k in keys.values()),
                          key=lambda k: k.get("created_at") or 0, reverse=True))


@app.route("/api-keys", methods=["POST"])
def api_keys_create():
    data = request.json or {}
    name = (data.get("name") or "").strip()[:60]
    scope = data.get("scope") or "lunadrome"
    if not name:
        return jsonify({"error": "Give the key a name (e.g. \"LunaDrome on my PC\")"}), 400
    if scope not in API_KEY_SCOPES:
        return jsonify({"error": f"Unknown scope - use one of: {', '.join(API_KEY_SCOPES)}"}), 400
    key = "sdk_" + secrets.token_urlsafe(32)
    now = time.time()
    record = {"id": uuid.uuid4().hex[:12], "name": name, "scope": scope, "prefix": key[:10],
              "hash": _hash_secret(key), "created_at": now, "created_by": _identify()["user"],
              "last_used_at": None}
    with _auth_lock:
        keys = _load_auth_store(API_KEYS_FILE)
        keys[record["id"]] = record
        _save_auth_store(API_KEYS_FILE, keys)
    # The only time the full key is ever returned - only its hash is stored.
    return jsonify({**_public_key_record(record), "key": key}), 201


@app.route("/api-keys/<key_id>", methods=["DELETE"])
def api_keys_revoke(key_id):
    with _auth_lock:
        keys = _load_auth_store(API_KEYS_FILE)
        if key_id not in keys:
            return jsonify({"error": "No such key"}), 404
        keys.pop(key_id)
        _save_auth_store(API_KEYS_FILE, keys)
    return jsonify({"ok": True})

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

def _mark_stale_jobs_interrupted():
    """A job left in a non-terminal status ('running' / 'scanning' /
    'uploading') on disk always means its worker thread died with the
    previous process (container restart, crash) — nothing resumes it, so
    without this it looks like a live job forever in the UI (and the /jobs
    list endpoint re-saving it on every poll just keeps that stale
    'running' state alive on disk too). Close it out honestly on startup
    instead of leaving a ghost job that never finishes."""
    changed = False
    for job in jobs.values():
        if job.get("status") in ("running", "scanning", "uploading"):
            job["status"] = "done"
            job["current_track"] = None
            job.setdefault("log", []).append(
                "⚠ Interrupted by a server restart — re-run this if it didn't finish")
            changed = True
    if changed:
        save_jobs()

_mark_stale_jobs_interrupted()

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

# ─── Long-track whitelist ───────────────────────────────────────────────────
# Separate from the permanent-ignore list above: whitelisting a track just
# hides it from the /library/long-tracks report (it's a legitimately long
# track, e.g. a DJ mix or a live medley) — it does NOT stop it from being
# (re)downloaded. Removing a long track is the opposite: it deletes the file
# and *also* adds it to the permanent-ignore list so it's never fetched again.

def load_long_track_whitelist():
    try:
        with open(LONG_TRACK_WHITELIST_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_long_track_whitelist(data):
    with open(LONG_TRACK_WHITELIST_FILE, "w") as f:
        json.dump(data, f, indent=2)

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
    """Get ALL filenames (basenames only) across entire music library in one
    SSH call — this is the source of truth every download worker's dedup
    check (_remote_duplicate_exists) runs against, so a failure here is
    silently catastrophic: an empty return makes EVERY track in the run
    look like it's missing, triggering a full redundant redownload of the
    whole library. 30s used to be the timeout; _VERIFY_SCAN_SCRIPT's
    comparable whole-library walk elsewhere in this file already needed
    180s of headroom on a small (low-CPU/low-RAM, swapping) Navidrome
    host, so 30s was thin margin even though a `find` over a few thousand
    files measured well under a second when the host was idle — under any
    real contention (a concurrent rsync batch, Navidrome's own scan) 30s
    isn't a safe bet. Widened to match, and logs loudly on failure instead
    of quietly returning an empty set with no trace anywhere but a
    swallowed exception."""
    try:
        cmd = ["ssh", "-i", "/root/.ssh/id_rsa",
               "-p", str(cfg["port"]),
               "-o", "StrictHostKeyChecking=no",
               "-o", "BatchMode=yes",
               "-o", "ConnectTimeout=5",
               f"{cfg['user']}@{cfg['host']}",
               f"find '{cfg['music_path']}' -name '*.flac' -printf '%f\n' 2>/dev/null"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        return set(result.stdout.strip().split("\n")) if result.stdout.strip() else set()
    except Exception as e:
        print(f"[dedup] get_all_remote_files failed — every track this run will look "
              f"'missing' and may be redownloaded: {e}", file=sys.stderr)
        return set()

def rsync_to_remote(local_dir, cfg, job_id=None, to_root=True):
    """Returns True/False — deliberately never raises. This used to let a
    subprocess.TimeoutExpired (or any other subprocess failure) propagate
    straight out of subprocess.run() uncaught. Nothing up the call chain
    catches that: batch_upload_and_cleanup -> download_worker/
    ytmusic_download_worker -> auto_sync_worker's per-playlist loop has no
    try/except either, so an exception here used to silently kill the
    entire nightly auto-sync thread mid-run — the playlist being rsynced
    stayed stuck at status 'uploading' forever (nothing ever set it to
    'done'), and every tracked playlist after it in that run never got
    its turn at all, with no error surfaced anywhere except a Python
    traceback in the container's stderr nobody was looking at. On an
    underpowered Navidrome host (little CPU/RAM, swapping under load)
    a big batch rsync taking longer than the
    600s timeout below is a completely realistic way to trigger this, and
    is exactly what happened in practice (auto-sync stuck on "rsyncing"
    — the stuck job's own next-restart cleanup
    mislabeled it as interrupted by a server restart, when the real cause
    was this uncaught timeout hours earlier)."""
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
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        log("✗ Rsync timed out after 10 min — the Navidrome host may be overloaded, try again later")
        return False
    except Exception as e:
        log(f"✗ Rsync failed to run: {e}")
        return False
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

# ─── Source Navidrome (someone else's server, browsed/pulled from) ─────────
# Distinct from NAVIDROME_CONFIG_FILE above, which is always *this* app's
# own sync destination. This is a second, independent Subsonic connection —
# typically another self-hosted Navidrome — used only to
# browse its library and pull tracks from it into the destination above.
# Same {url, user, password} shape, reuses nd_subsonic() by passing this
# config in explicitly.

def load_source_nd_config():
    try:
        with open(SOURCE_NAVIDROME_CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return None

def save_source_nd_config(url, user, password):
    with open(SOURCE_NAVIDROME_CONFIG_FILE, "w") as f:
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

def batch_upload_and_cleanup(local_dir, ssh_cfg, nd_cfg, playlist_name, batch_tracks, job_id,
                             sync_playlist=True):
    """Rsync current downloads to Navidrome, trigger scan, sync playlist (unless
    sync_playlist=False), delete local files."""
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
            if sync_playlist:
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
                           "track_number": t.get("track_number"),
                           "disc_number": t.get("disc_number"),
                           "duration_ms": t["duration_ms"],
                           "image": t["album"]["images"][0]["url"] if t["album"].get("images") else None})
        if not batch["next"]: break
        offset += 100
    return tracks

# ─── Core download helpers ────────────────────────────────────────────────────

def sanitize(name):
    return re.sub(r'[\\/*?:"<>|]', "_", name)

# ─── Album naming consistency ─────────────────────────────────────────────────
# Streaming services rename and re-spell releases over time - e.g. a
# soundtrack series first published as "X, Vol. 5 (Music from ...)" and
# later retitled "Songs Part Five", or the same album spelled "Rwby" in one
# source and "RWBY" in another. Downloads just copied whatever name the
# source had at that moment, so one album could end up split across several
# names (and folders) in the library. canonical_album() picks the name a new
# track's album gets, consistent with what the library already has.
ALBUM_ALIASES_FILE = "/root/.ssh/album_aliases.json"


def get_remote_album_dirs(cfg):
    """Names of the top-level album folders in the music library (one SSH call)."""
    try:
        cmd = ["ssh", "-i", "/root/.ssh/id_rsa", "-p", str(cfg["port"]),
               "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
               f"{cfg['user']}@{cfg['host']}",
               f"find '{cfg['music_path']}' -mindepth 1 -maxdepth 1 -type d -printf '%f\\n' 2>/dev/null"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return {line for line in result.stdout.splitlines() if line.strip()}
    except Exception as e:
        print(f"[albums] listing album folders failed: {e}", file=sys.stderr)
        return set()


def load_album_naming(ssh_cfg):
    """What canonical_album() needs, loaded once per download run: the rename
    rules from ALBUM_ALIASES_FILE and the library's existing album folders.

    album_aliases.json maps a source album name (case-insensitive) to the name
    it should get in this library, either as a plain string or as
      {"album": "<album tag>", "folder": "<existing folder, if its name differs>",
       "only_artist": "<only when the (album) artist contains this>" (or a list)}"""
    try:
        with open(ALBUM_ALIASES_FILE) as f:
            aliases = {k.strip().lower(): v for k, v in json.load(f).items()}
    except FileNotFoundError:
        aliases = {}
    except Exception as e:
        print(f"[albums] {ALBUM_ALIASES_FILE} unreadable, ignoring it: {e}", file=sys.stderr)
        aliases = {}
    folders = get_remote_album_dirs(ssh_cfg) if ssh_cfg else set()
    return {"aliases": aliases, "folders": {f.lower(): f for f in folders}}


def canonical_album(album, naming, artist=None):
    """(album tag, folder name) for a new track's album:
    1. a matching rename rule from album_aliases.json wins;
    2. else an existing album folder whose name only differs in upper/lower
       case is reused, with that spelling as the album tag too;
    3. else the name as given (folder = the sanitized name)."""
    album = (album or "").strip() or "Unknown Album"
    if not naming:
        return album, sanitize(album)
    rule = naming["aliases"].get(album.lower())
    if isinstance(rule, str):
        rule = {"album": rule}
    if rule and rule.get("album"):
        only = rule.get("only_artist") or []
        only = [only] if isinstance(only, str) else only
        if not only or any(o.strip().lower() in (artist or "").lower() for o in only):
            return rule["album"], rule.get("folder") or sanitize(rule["album"])
    existing = naming["folders"].get(sanitize(album).lower())
    if existing and existing != sanitize(album):
        # Only borrow the folder's spelling as the tag when sanitizing didn't
        # change the name (a folder can't hold characters like ':' or '/').
        return (existing if sanitize(album) == album else album), existing
    return album, sanitize(album)

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

# yt-dlp's --add-metadata embeds a bunch of the source video's own fields
# automatically, and one it happily writes straight into the GENRE tag is
# YouTube's own video *category* — "Music", "People & Blogs", "Gaming",
# etc. That's a completely different, much coarser classification than a
# music genre (every music video on the platform is generically "Music"),
# and it isn't produced by lookup_genre()/YT_GENRE_KEYWORDS above at all —
# it's already sitting in the file the moment yt-dlp finishes downloading
# it, before any of this app's own genre logic ever runs. Both fix_tags()
# and the genre-relabel job explicitly clear it out on sight (rather than
# just never fixing it further, which is all they used to do whenever a
# real genre couldn't be found to replace it with), since it's never a
# genre a track should keep no matter how confident the source is.
YT_VIDEO_CATEGORIES = {
    "film & animation", "autos & vehicles", "music", "pets & animals",
    "sports", "short movies", "travel & events", "gaming", "videoblogging",
    "people & blogs", "comedy", "entertainment", "news & politics",
    "howto & style", "education", "science & technology",
    "nonprofits & activism", "movies", "anime/animation", "action/adventure",
    "classics", "documentary", "drama", "family", "foreign", "horror",
    "sci-fi/fantasy", "thriller", "shorts", "shows", "trailers",
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

def fix_tags(filepath, title, artist, album, album_artist=None, source_url=None, genre=None,
             track_number=None, disc_number=None):
    album_artist = album_artist or artist
    try:
        if filepath.endswith('.flac'):
            tags = FLAC(filepath)
            tags["title"] = [title]
            tags["artist"] = [artist]
            tags["album"] = [album]
            tags["albumartist"] = [album_artist]
            if track_number:
                tags["tracknumber"] = [str(track_number)]
            if disc_number:
                tags["discnumber"] = [str(disc_number)]
            if source_url:
                tags["comment"] = [source_url]
            if genre:
                tags["genre"] = [genre]
            elif (tags.get("genre") or [""])[0].strip().lower() in YT_VIDEO_CATEGORIES:
                # No real genre to set, but whatever's already there is a
                # raw YouTube video category (yt-dlp's own auto-embedded
                # metadata, not a music genre at all) — clear it rather
                # than silently keep it just because nothing better was found.
                del tags["genre"]
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
            if track_number:
                tags["TRCK"] = TRCK(encoding=3, text=str(track_number))
            if disc_number:
                tags["TPOS"] = TPOS(encoding=3, text=str(disc_number))
            if source_url:
                tags["COMM"] = COMM(encoding=3, lang="eng", desc="", text=source_url)
            if genre:
                tags["TCON"] = TCON(encoding=3, text=genre)
            elif str(tags.get("TCON", "")).strip().lower() in YT_VIDEO_CATEGORIES:
                del tags["TCON"]
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

class _TimeoutSession(http.Session):
    """requests session for ytmusicapi with a default timeout. ytmusicapi never
    passes one, so during a DNS/network outage (seen 2026-09-25: dockerd failing
    to reach the upstream resolver) every call could block its thread forever -
    the ThreadPoolExecutor timeouts around the calls only stop *waiting*, the
    worker threads stay stuck and pile up."""
    def request(self, *args, **kwargs):
        kwargs.setdefault("timeout", 15)
        return super().request(*args, **kwargs)

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
                _ytmusic_client = YTMusic(requests_session=_TimeoutSession())
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

# ─── "Is this already in the library?" dedup check ─────────────────────────
# Every filename in this library is written as sanitize(f"{artist} - {name}")
# (see download_worker/ytmusic_download_worker below, and the manual-retry/
# flag-wrong paths). The dedup check used to just be a plain case-sensitive
# `any(f.startswith(filename) for f in remote_files_flat)` against that exact
# string — brittle to any artist-credit formatting difference between
# sources: Spotify's playlist_tracks() joins EVERY credited artist with ", "
# (e.g. "Missy Elliott, Timbaland"), while a raw YouTube playlist import only
# ever has the uploader/channel name (typically just the primary artist,
# e.g. "Missy Elliott"), and manual URL-paste retries can differ again. A
# track saved via one path and later encountered via another silently failed
# the exact match and got redownloaded from scratch — confirmed real cause
# of a large playlist sync redownloading things already in the library.
# Fixed by reusing the same _title_ok/_artist_ok fuzzy
# matchers already trusted elsewhere in this file, indexed by normalized
# title first so a per-track check stays cheap (O(1) bucket lookup) instead
# of rescanning the whole remote file list per track — important given the
# whole point here is to stop syncs from taking forever, not add more work.

def _index_remote_files_by_title(remote_filenames):
    """{normalized_title: [(artist_guess, title_guess, raw_filename), ...]}
    — parses each 'Artist - Title.ext' filename by splitting on the FIRST
    ' - ' (the exact inverse of how it was joined), so an artist or title
    that itself legitimately contains ' - ' still splits correctly."""
    index = {}
    for f in remote_filenames:
        stem = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", f)  # strip the extension
        if " - " not in stem:
            continue
        artist_guess, title_guess = stem.split(" - ", 1)
        index.setdefault(_normalize_title(title_guess), []).append((artist_guess, title_guess, f))
    return index

def _remote_duplicate_exists(track, remote_title_index):
    """track: a dict with 'name' and 'artist' keys (same shape used
    throughout download_worker/ytmusic_download_worker)."""
    for artist_guess, title_guess, _f in remote_title_index.get(_normalize_title(track["name"]), []):
        if _title_ok(title_guess, track["name"]) and _artist_ok([artist_guess], track["artist"]):
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

# Manual retries and pasted-URL imports bypass the scored multi-provider
# matcher entirely (the whole point is the user hands us an exact URL), so
# nothing there ever ran _looks_like_non_music or a duration check against
# it. In practice that let things like a full "let's play" episode get saved
# and tagged as a 2-minute game OST track (confirmed: several game-soundtrack
# tracks turned out to be 20-30min commentary videos this way). This
# is the safety net for both of those paths, applied *after* download so it
# can inspect the file yt-dlp actually produced instead of trusting the URL.
#
# It also runs (with an expected_artist) after every candidate download in
# the normal scored-matcher path (download_worker) — belt and suspenders on
# top of _artist_ok's pre-download channel-name check, since that only sees
# the channel/uploader name, not the richer "artist" field yt-dlp's own
# --add-metadata pulls from the video itself. Confirmed necessary: several
# tracks of one album turned out to be a completely different artist's
# same-titled song (the downloaded file's own TAG:artist named the other
# artist) - a title collision _title_ok alone can't catch.
DURATION_MISMATCH_RATIO = 2.5
DURATION_MISMATCH_FLOOR_SEC = 45

def _downloaded_file_looks_wrong(flac_path, expected_title, expected_duration_ms=0, expected_artist=None):
    """Sanity-check a just-downloaded FLAC against what it was supposed to
    be. Returns a human-readable reason string if it looks wrong, else None.
    Must be called BEFORE fix_tags() overwrites the file's own tags — this
    reads yt-dlp's originally-embedded title/description/artist to see what
    the source video actually was."""
    try:
        tags = FLAC(flac_path)
    except Exception:
        return None  # can't inspect it; don't block on our own failure to read it

    actual_sec = tags.info.length if tags.info else 0
    if expected_duration_ms:
        expected_sec = expected_duration_ms / 1000
        if (actual_sec > expected_sec * DURATION_MISMATCH_RATIO
                and actual_sec - expected_sec > DURATION_MISMATCH_FLOOR_SEC):
            return (f"downloaded audio is {actual_sec/60:.1f} min long but "
                    f"'{expected_title}' should be about {expected_sec/60:.1f} min — "
                    f"this is very likely the wrong video (e.g. a full soundtrack, "
                    f"let's play/walkthrough, or extended mix), not the actual track")

    raw_title = (tags.get("title") or [""])[0]
    raw_desc = (tags.get("synopsis") or tags.get("description") or [""])[0]
    if _looks_like_non_music(f"{raw_title} {raw_desc}", expected_title):
        return (f"the source video's own title/description looks like non-music "
                f"content (podcast, let's play, walkthrough episode, etc.), not "
                f"'{expected_title}'")

    # yt-dlp's --add-metadata embeds the video's own "artist" field (distinct
    # from uploader/channel) whenever the source actually has one — reliably
    # true for official music uploads. When it's present and it flatly
    # doesn't match who this was supposed to be by, a title collision with a
    # different artist's same-named song is the near-certain explanation.
    # Absent/empty is not evidence of anything (plenty of legitimate uploads
    # carry no artist tag at all) — only a confident mismatch rejects.
    raw_artist = (tags.get("artist") or [""])[0].strip()
    if raw_artist and expected_artist and not _artist_ok([raw_artist], expected_artist):
        return (f"the source video's own artist tag is '{raw_artist}', not "
                f"'{expected_artist}' — almost certainly a different artist's "
                f"same-titled song, not '{expected_title}'")
    return None

def _embedded_artist_tag(flac_path):
    """Read a just-downloaded FLAC's own --add-metadata-embedded artist tag
    — same field _downloaded_file_looks_wrong() reads, before fix_tags()
    overwrites it. Used by the raw YouTube playlist/album/video import path
    (ytmusic_download_worker), where the only 'artist' available at
    --flat-playlist listing time is the channel/uploader name (with an
    " - Topic" suffix stripped) — a guess that's flatly wrong whenever the
    uploading Topic channel isn't named after the actual performer (found
    in production: 8 tracks across unrelated albums/genres all tagged
    artist "Release", because YouTube's Content ID system had grouped them
    under a Topic channel literally named "Release" rather than the real
    performer — confirmed via a full yt-dlp resolve, e.g. the Metal Gear
    Rising Revengeance track came back with the real artist tag "Free
    Dominguez, Logan Mader/Jamie Christopherson"). yt-dlp's full resolution
    at actual download time routinely surfaces that real artist credit even
    when the flat listing never had it — strictly better ground truth than
    a channel name once it's available, so prefer it when non-empty."""
    try:
        tags = FLAC(flac_path)
    except Exception:
        return None
    raw = (tags.get("artist") or [""])[0].strip()
    return raw or None

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

def maybe_correct_album(flac_path, title, artist, album, playlist_name, source_url, local_dir, album_artist=None,
                        naming=None):
    """If album looks like a placeholder (empty/'Unknown Album'/the playlist name
    itself), look up the real album via yt-dlp and move the file into the
    corrected album folder. Returns (album, flac_path), updated if corrected."""
    normalized = (album or "").strip().lower()
    if normalized not in BAD_ALBUM_VALUES and normalized != (playlist_name or "").strip().lower():
        return album, flac_path
    real_album = lookup_real_album(source_url)
    if not real_album or real_album.strip().lower() == normalized:
        return album, flac_path
    real_album, real_folder = canonical_album(real_album, naming, album_artist or artist)
    try:
        new_album_dir = os.path.join(local_dir, real_folder)
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
    - nice 15 so gunicorn stays responsive
    - Hard wall-clock deadline
    - Skip flag support
    - Full process group kill on timeout/skip
    Returns (returncode, killed_reason, stdout, stderr) where killed_reason is None on success
    """
    # New session + low priority without preexec_fn: running Python code in the
    # forked child (what preexec_fn does) can deadlock in this multi-threaded
    # gunicorn worker, per the subprocess docs. start_new_session does the
    # setsid() in C before exec, and `nice` is applied by the nice binary.
    proc = subprocess.Popen(["nice", "-n", "15"] + list(cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)
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
            # Never signal our own group (gunicorn itself) - only the child's session.
            pgid = os.getpgid(proc.pid)
            if pgid == os.getpgrp():
                raise ProcessLookupError("child still in gunicorn's process group")
            os.killpg(pgid, signal.SIGKILL)
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
    remote_title_index = _index_remote_files_by_title(remote_files_flat)
    naming = load_album_naming(ssh_cfg)

    for i, track in enumerate(tracks):
        with job_lock:
            jobs[job_id]["current"] = i + 1
            jobs[job_id]["current_track"] = f"{track['artist']} - {track['name']}"

        if is_track_ignored(track['artist'], track['name']):
            with job_lock:
                jobs[job_id]["log"].append(f"🚫 Permanently ignored: {track['artist']} - {track['name']}")
            continue

        filename = sanitize(f"{track['artist']} - {track['name']}")
        source_album = track['album']
        track['album'], album_folder = canonical_album(
            source_album, naming, track.get('album_artist') or track['artist'])
        album_dir = os.path.join(local_dir, album_folder)
        os.makedirs(album_dir, exist_ok=True)
        out_template = os.path.join(album_dir, f"{filename}.%(ext)s")

        # Check remote globally (any folder) — fuzzy match, see
        # _remote_duplicate_exists' docstring for why this isn't a plain
        # exact filename check.
        if _remote_duplicate_exists(track, remote_title_index):
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
                wrong_reason = _downloaded_file_looks_wrong(
                    flac_path, track['name'], track.get('duration_ms', 0), expected_artist=track['artist'])
                if wrong_reason:
                    try:
                        os.remove(flac_path)
                    except Exception:
                        pass
                    with job_lock:
                        jobs[job_id]["log"].append(f"✗ Rejected via {provider} ({wrong_reason}): {label}")
                    last_reason = f"rejected via {provider} ({wrong_reason})"
                    continue
                source_url = extract_resolved_url(stdout)
                track["source_url"] = source_url
                genre = lookup_genre(track['artist'])
                if source_album and source_album != track['album']:
                    with job_lock:
                        jobs[job_id]["log"].append(
                            f"🏷 Album: {source_album} → {track['album']} (as it's named in the library)")
                fix_tags(flac_path, track['name'], track['artist'], track['album'],
                         album_artist=track.get('album_artist'), source_url=source_url, genre=genre,
                         track_number=track.get('track_number'), disc_number=track.get('disc_number'))
                new_album, flac_path = maybe_correct_album(
                    flac_path, track['name'], track['artist'], track['album'],
                    playlist_name, source_url, local_dir, album_artist=track.get('album_artist'),
                    naming=naming)
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

def ytmusic_download_worker(job_id, url, playlist_name, is_playlist=False,
                            sync_playlist=True, track_for_sync=True, album_hint=None,
                            complete_album=False, album_artist_hint=None):
    # complete_album / album_artist_hint: see album_mode below - a whole album into
    # one folder, tagged with one album artist + track numbers.
    # sync_playlist / track_for_sync: LunaDrome's "Download via SpotiDrome" sends
    # False for both — it only wants the songs/album in the library, not a
    # Navidrome playlist named after the download or an auto-sync entry.
    # album_hint: the album name when the caller already knows it (a YT Music
    # album picked in LunaDrome), used instead of "Unknown Album" when the flat
    # listing doesn't say.
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
        if is_playlist and track_for_sync:
            record_sync_health(yt_playlist_id(url), 0, 0, 0, status="failed")
        return

    with job_lock:
        jobs[job_id]["total"] = len(entries)
        jobs[job_id]["log"].append(f"ℹ Found {len(entries)} track(s)")

    downloaded_tracks = []
    yt_track_list = []

    # complete_album (LunaDrome album downloads): the album should end up whole in
    # its own album folder. Only a copy already *in that folder* counts as a
    # duplicate - a track that exists elsewhere (a single, a compilation) still
    # gets downloaded into the album. So an album that's partly in the library
    # gets its missing tracks added; one that isn't gets created with all of them.
    album_mode = bool(complete_album and album_hint)
    naming = load_album_naming(ssh_cfg)
    if album_mode:
        # e.g. LunaDrome asks for YT Music's "Songs Part Five": land in (and
        # dedupe against) the album the library already has under another name.
        album_hint, album_hint_folder = canonical_album(album_hint, naming, album_artist_hint)

    if album_mode and ssh_cfg:
        album_remote_dir = f"{ssh_cfg['music_path']}/{album_hint_folder}"
        remote_title_index = _index_remote_files_by_title(get_remote_files(album_remote_dir, ssh_cfg))
    else:
        # Get ALL remote files once (global dedup across all playlists)
        remote_files_flat = get_all_remote_files(ssh_cfg) if ssh_cfg else set()
        remote_title_index = _index_remote_files_by_title(remote_files_flat)

    for i, entry in enumerate(entries):
        track_id = entry.get("id") or entry.get("url", "")
        title = entry.get("title", "Unknown Title")
        artist = entry.get("uploader") or entry.get("channel") or entry.get("artist") or "Unknown Artist"
        artist = re.sub(r" - Topic$", "", artist)
        if album_mode:
            # every track in the one album folder, under one album name
            album, album_folder = album_hint, album_hint_folder
        else:
            album, album_folder = canonical_album(
                entry.get("album") or entry.get("release") or album_hint or "Unknown Album", naming, artist)
        track_url = f"https://www.youtube.com/watch?v={track_id}" if not track_id.startswith("http") else track_id

        with job_lock:
            jobs[job_id]["current"] = i + 1
            jobs[job_id]["current_track"] = f"{artist} - {title}"

        if is_track_ignored(artist, title):
            with job_lock:
                jobs[job_id]["log"].append(f"🚫 Permanently ignored: {artist} - {title}")
            continue

        # A pasted single track URL is an explicit, deliberate choice — nothing
        # to second-guess it against. A pasted *playlist/album* URL is swept in
        # wholesale with no per-entry review, though, and playlists like game-OST
        # "full soundtrack" rips are commonly interleaved with unrelated content
        # (let's plays, trailers, etc.) by whoever uploaded them — so filter
        # those the same way the Spotify path's matcher does.
        # (not for a LunaDrome album: an official YT Music album is curated, and a real
        # track can be called "Review" or "Episode 1")
        if is_playlist and not album_mode and any(kw in title.lower() for kw in NOT_MUSIC_KEYWORDS):
            with job_lock:
                jobs[job_id]["log"].append(f"🚫 Skipped (looks like non-music content): {artist} - {title}")
            continue

        filename = sanitize(f"{artist} - {title}")
        album_dir = os.path.join(local_dir, album_folder)
        os.makedirs(album_dir, exist_ok=True)
        out_template = os.path.join(album_dir, f"{filename}.%(ext)s")
        t = {"id": track_id, "name": title, "artist": artist, "album": album, "duration_ms": 0, "image": None, "source_url": track_url}

        # Check remote globally (any folder) — fuzzy match, see
        # _remote_duplicate_exists' docstring for why this isn't a plain
        # exact filename check.
        if _remote_duplicate_exists(t, remote_title_index):
            with job_lock:
                jobs[job_id]["log"].append(
                    f"⏭ Already in this album: {artist} - {title}" if album_mode
                    else f"⏭ Already on Navidrome: {artist} - {title}")
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
                batch_upload_and_cleanup(local_dir, ssh_cfg, nd_cfg, playlist_name, list(downloaded_tracks), job_id,
                                         sync_playlist=sync_playlist)
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
            if is_playlist and not album_mode:
                wrong_reason = _downloaded_file_looks_wrong(flac_path, title)
                if wrong_reason:
                    try:
                        os.remove(flac_path)
                    except Exception:
                        pass
                    record_failed_track(failed_track_stub, failed_playlist_id, playlist_name,
                                         f"looks like non-music content ({wrong_reason})")
                    with job_lock:
                        jobs[job_id]["log"].append(f"✗ Rejected ({wrong_reason}): {artist} - {title}")
                        jobs[job_id]["failed"] += 1
                    continue
            # The "artist" computed above is only ever the channel/uploader
            # name at --flat-playlist listing time (see the top of this
            # loop) — a guess that's flatly wrong whenever that channel
            # isn't named after the real performer. The actual download
            # just did a full yt-dlp resolve, which often surfaces a real
            # artist credit the flat listing never had — prefer it now,
            # before fix_tags() overwrites the file's own tag with whatever
            # we pass it. See _embedded_artist_tag()'s docstring for the
            # "Release" mistagging this was found from.
            better_artist = _embedded_artist_tag(flac_path)
            if better_artist and better_artist != artist:
                with job_lock:
                    jobs[job_id]["log"].append(
                        f"🏷 Corrected artist: '{artist}' → '{better_artist}' (from the source's own metadata)")
                artist = better_artist
                t["artist"] = artist
            if album_mode:
                # One album artist for every track (a featured artist on one track
                # would otherwise split it off into its own album in Navidrome), the
                # album's genre, and the track number from the album's order.
                album_artist = album_artist_hint or artist
                genre = lookup_genre(album_artist)
                fix_tags(flac_path, title, artist, album, album_artist=album_artist,
                         source_url=track_url, genre=genre, track_number=i + 1)
                new_album = album  # the album is known - no correction guessing
            else:
                genre = lookup_genre(artist)
                fix_tags(flac_path, title, artist, album, source_url=track_url, genre=genre)
                new_album, flac_path = maybe_correct_album(
                    flac_path, title, artist, album, playlist_name, track_url, local_dir, naming=naming)
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
                batch_upload_and_cleanup(local_dir, ssh_cfg, nd_cfg, playlist_name, list(downloaded_tracks), job_id,
                                         sync_playlist=sync_playlist)
        else:
            reason = _extract_yt_dlp_error(stderr) or f"yt-dlp exited with code {rc}"
            record_failed_track(failed_track_stub, failed_playlist_id, playlist_name, reason)
            with job_lock:
                jobs[job_id]["log"].append(f"✗ Failed ({reason}): {artist} - {title}")
                jobs[job_id]["failed"] += 1

    if is_playlist and yt_track_list and track_for_sync:
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
        if sync_playlist:
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
    if is_playlist and track_for_sync:
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

            # Each playlist's whole download+sync is wrapped here on purpose.
            # This loop used to have no try/except at all — an uncaught
            # exception anywhere down the call chain (confirmed cause: a
            # slow rsync exceeding rsync_to_remote's 600s timeout, entirely
            # plausible on an underpowered Navidrome host under
            # load) would kill this whole background thread right then and
            # there. That left the in-progress job stuck in a non-terminal
            # status forever (nothing downstream ever set it to "done"),
            # and — worse — silently skipped every tracked playlist that
            # hadn't had its turn yet that night, with no record of it
            # anywhere but a traceback in the container's stderr (GitHub
            # issue: "Auto Sync staying stuck on RSyncing"). One playlist's
            # failure must never take the rest of the night down with it.
            try:
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
            except Exception as e:
                print(f"[auto-sync] {playlist_name} failed unexpectedly, moving on to the rest of tonight's playlists: {e}",
                      file=sys.stderr)
                with job_lock:
                    if job_id in jobs and jobs[job_id].get("status") not in ("done",):
                        jobs[job_id]["log"].append(f"✗ Sync failed unexpectedly: {e}")
                        jobs[job_id]["status"] = "done"
                        jobs[job_id]["current_track"] = None
                    save_jobs()

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

def _album_folder_name(path):
    return os.path.basename(os.path.dirname(path))

def _is_edition_sibling_pair(path_a, path_b):
    """True when path_a/path_b sit in two different album folders that are
    the same release under Consolidate Editions' own definition (e.g.
    "Lover Of A Ghost" vs "Lover Of A Ghost (Deluxe Version)") — i.e. two
    genuinely different releases that deliberately share a track, not an
    accidental duplicate. Both dedupe sweeps below delete the smaller/
    non-kept file of a matched pair; without this exemption they silently
    undo Consolidate Editions' whole point the very next time they run,
    which is exactly what happened to a real album (2026-08-27) — this
    reuses the same base-name-after-stripping-edition-suffix logic
    Consolidate Editions itself uses to decide what counts as a sibling,
    defined further down as _edition_base_name (forward reference — fine
    at call time, both are top-level functions in the same module)."""
    folder_a, folder_b = _album_folder_name(path_a), _album_folder_name(path_b)
    if folder_a == folder_b:
        return False  # same album folder — an ordinary duplicate, not this case
    base_a, base_b = _edition_base_name(folder_a), _edition_base_name(folder_b)
    return bool(base_a) and base_a.lower() == base_b.lower()

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
        keep = files[0]
        for f in files[1:]:
            if _is_edition_sibling_pair(keep["path"], f["path"]):
                continue  # deliberately duplicated across sibling editions — leave both
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
        result = _run_remote_scan(cmd, timeout=300)
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
                if _is_edition_sibling_pair(keep["path"], d["path"]):
                    continue  # deliberately duplicated across sibling editions — leave both
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
            if genre:
                tags["genre"] = [genre]
            elif "genre" in tags:
                del tags["genre"]
            tags.save()
        elif p.endswith(".mp3"):
            try:
                tags = ID3(p)
            except ID3Error:
                tags = ID3()
            if genre:
                tags["TCON"] = TCON(encoding=3, text=genre)
            elif "TCON" in tags:
                del tags["TCON"]
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

def _run_remote_scan(cmd, timeout=180, retries=1, retry_delay=5, **run_kwargs):
    """subprocess.run() wrapper for the remote "python3 -c <scan script>"
    SSH calls (genre scan, title-dedupe scan, orphan scan, verify scan) —
    retries once on failure before giving up. Added after a one-off, never
    reproduced 'ModuleNotFoundError: No module named mutagen' failure on
    the Navidrome host — nothing on that host's package/apt/dpkg logs showed any actual
    change around the failure time, so the working theory is a transient
    hiccup (a small host swapping under load) rather than a real, lasting
    environment break. A single retry costs nothing when the scan already
    succeeds, and turns a one-off blip into a non-event instead of a
    failed job someone has to notice and manually re-run."""
    last_result = None
    for attempt in range(retries + 1):
        last_result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **run_kwargs)
        if last_result.returncode == 0:
            return last_result
        if attempt < retries:
            print(f"[remote-scan] attempt {attempt + 1} failed (rc={last_result.returncode}), "
                  f"retrying in {retry_delay}s: {last_result.stderr[-300:]}", file=sys.stderr)
            time.sleep(retry_delay)
    return last_result

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
    pruned_files = 0
    file_error = None
    if orphans:
        ids_str = ",".join(f"'{o['id']}'" for o in orphans)
        del_cmd = _ssh_cmd(ssh_cfg,
            f"sqlite3 /var/lib/navidrome/navidrome.db \"DELETE FROM media_file WHERE id IN ({ids_str});\"")
        result = subprocess.run(del_cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            pruned_files = len(orphans)
        else:
            file_error = result.stderr[-300:]

    # Navidrome's own `album` table can also be left with rows that have zero
    # media_file rows pointing at them (found while building edition
    # consolidation: ~8% of a real library at the time). These are only
    # *counted* here, never deleted directly: Navidrome (0.61) purges empty
    # albums itself during the scan below - verified on a throwaway library.
    # A direct `DELETE FROM album` with the host's sqlite3 CLI is unsafe: the
    # album_updated_at / album_created_at indexes are on datetime(...), and the
    # CLI's SQLite rounds fractional seconds >= .9995 up where Navidrome's
    # bundled SQLite doesn't, so the two compute different index keys. That
    # mismatch broke every Navidrome scan that reached an affected album with
    # "database disk image is malformed" from 2026-08-26 until 2026-09-25.
    def _count_stale_albums():
        count_cmd = _ssh_cmd(ssh_cfg,
            "sqlite3 -readonly /var/lib/navidrome/navidrome.db "
            "\"SELECT COUNT(*) FROM album WHERE id NOT IN (SELECT DISTINCT album_id FROM media_file);\"")
        result = subprocess.run(count_cmd, capture_output=True, text=True, timeout=20)
        if result.returncode != 0:
            return None, result.stderr[-300:]
        try:
            return int(result.stdout.strip()), None
        except ValueError:
            return None, f"unexpected album count output: {result.stdout[-100:]!r}"

    stale_albums, album_error = _count_stale_albums()

    pruned_albums = 0
    if (pruned_files or stale_albums) and nd_cfg:
        ok, _msg = nd_trigger_scan(nd_cfg, full=True)
        if ok:
            nd_wait_for_scan(nd_cfg, timeout=300)
            if stale_albums:
                remaining, err = _count_stale_albums()
                if remaining is not None:
                    pruned_albums = max(stale_albums - remaining, 0)
                album_error = album_error or err

    return {"pruned": pruned_files, "entries": orphans, "pruned_albums": pruned_albums,
            "error": file_error or album_error}

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
        result = _run_remote_scan(scan_cmd, timeout=180)
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

    updates, processed, already_correct, no_genre_found, cleared_junk = [], 0, 0, 0, 0
    for artist, group in by_artist.items():
        with job_lock:
            if jobs[job_id].get("cancel_requested"):
                jobs[job_id]["log"].append(f"🛑 Cancelled ({processed}/{len(files)} scanned)")
                jobs[job_id]["status"] = "done"
                jobs[job_id]["current_track"] = None
                save_jobs()
                return
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
            # No real genre to set, but still worth a pass: yt-dlp's own
            # auto-embedded metadata writes YouTube's raw video *category*
            # ("Music", "People & Blogs", "Gaming"...) straight into the
            # genre tag at download time, and that's never actually a
            # music genre — clear it on sight even with nothing to
            # replace it with, rather than leaving it looking like a real
            # (wrong) answer forever just because nothing better turned up.
            for f in group:
                if (f.get("genre") or "").strip().lower() in YT_VIDEO_CATEGORIES:
                    updates.append({"path": f["path"], "genre": ""})
                    cleared_junk += 1
            no_genre_found += len(group) - sum(1 for f in group if (f.get("genre") or "").strip().lower() in YT_VIDEO_CATEGORIES)
            continue
        for f in group:
            if (f.get("genre") or "").strip().lower() == new_genre.lower():
                already_correct += 1
                continue
            updates.append({"path": f["path"], "genre": new_genre})

    with job_lock:
        jobs[job_id]["log"].append(
            f"ℹ {len(updates)} file(s) need a genre update ({cleared_junk} just clearing a YouTube "
            f"category that was never a real genre), {already_correct} already correct, "
            f"{no_genre_found} had no genre match on Spotify or YouTube")

    applied, failed = 0, 0
    if updates:
        with job_lock:
            jobs[job_id]["current_track"] = f"Writing {len(updates)} genre tag(s) on the Navidrome host…"
        BATCH = 200
        for i in range(0, len(updates), BATCH):
            with job_lock:
                if jobs[job_id].get("cancel_requested"):
                    jobs[job_id]["log"].append(f"🛑 Cancelled ({applied}/{len(updates)} tag(s) written)")
                    jobs[job_id]["status"] = "done"
                    jobs[job_id]["current_track"] = None
                    save_jobs()
                    return
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
# downloaded before that existed. This sweeps the whole library and only
# touches files whose measured loudness actually falls outside the target,
# re-encoding those in place while preserving every tag and embedded
# picture exactly as they were.
#
# 2026-08-27: this used to run entirely over SSH on the Navidrome host —
# one Python process per file, doing both the measure and encode ffmpeg
# passes remotely. Very slow in practice, and the reason turned out to be
# the Navidrome host itself: a single-core, low-RAM box, so
# thousands of files' worth of CPU-bound ffmpeg work was always going to run at
# whatever a single core allows, one file after another, no matter how the
# code was written. This container has 2 cores and isn't also trying to
# stay responsive for live streaming at the same time — so the actual
# audio work (both ffmpeg passes) now happens HERE instead, with 2 files
# in flight at once to actually use both cores. Only a small download-then-
# upload trip goes over SSH per file now, not the CPU-bound part.
_LOUDNORM_TARGET_I = float(re.search(r"I=(-?[\d.]+)", LOUDNORM_FILTER).group(1))
_LOUDNORM_TOLERANCE_LU = 1.0  # skip files already within 1 LU of the target
NORMALIZE_TMP_DIR = os.path.join(DOWNLOAD_DIR, "_normalize_tmp")
NORMALIZE_WORKERS = 2  # matches this container's own CPU limit (see docker-compose.yml) —
                       # the Navidrome host has only 1 core, which was the actual bottleneck


# Prefix for the local ffmpeg calls below - this container also serves live
# web/stream requests while up to NORMALIZE_WORKERS of these run, so the
# CPU-bound encode work shouldn't get to starve it. (A `nice` prefix rather
# than preexec_fn=os.nice: preexec_fn can deadlock in a multi-threaded process.)
_NICE = ["nice", "-n", "10"]


def _measure_loudness(local_path):
    cmd = ["ffmpeg", "-i", local_path, "-af", LOUDNORM_FILTER + ":print_format=json",
           "-vn", "-f", "null", "-"]
    r = subprocess.run(_NICE + cmd, capture_output=True, text=True, timeout=180)
    start = r.stderr.rfind("{")
    end = r.stderr.find("}", start) if start != -1 else -1
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(r.stderr[start:end + 1])
    except Exception:
        return None


def _normalize_one_file_local(ssh_cfg, rel_path):
    """Downloads one file, measures it, and — only if it's actually
    outside the target — re-encodes it locally and uploads just the
    result, finishing with the exact same safety properties the old
    remote-only version had: a same-directory temp file on the Navidrome
    side (so the final swap is an atomic same-filesystem rename, not a
    cross-filesystem copy), permissions explicitly copied from the
    original file rather than left at whatever a fresh file defaults to
    (see the ~74%-of-the-library-unplayable incident this app already had
    from getting exactly that wrong), and the original never touched
    unless a full, verified replacement is ready. Returns a result dict
    shaped like {"action": "normalized"|"skipped"|"failed", ...}."""
    remote_path = f"{ssh_cfg['music_path']}/{rel_path}"
    ext = os.path.splitext(rel_path)[1].lower()
    if ext not in (".flac", ".mp3"):
        return {"action": "skipped", "reason": "unsupported format", "rel_path": rel_path}

    os.makedirs(NORMALIZE_TMP_DIR, exist_ok=True)
    local_in = os.path.join(NORMALIZE_TMP_DIR, f"{uuid.uuid4().hex}{ext}")
    local_out = None
    try:
        scp_down = ["scp", "-i", "/root/.ssh/id_rsa", "-P", str(ssh_cfg["port"]),
                    "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
                    f"{ssh_cfg['user']}@{ssh_cfg['host']}:{remote_path}", local_in]
        r = subprocess.run(scp_down, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return {"action": "failed", "reason": f"download failed: {r.stderr[-200:]}", "rel_path": rel_path}

        summary = _measure_loudness(local_in)
        if summary is None:
            return {"action": "failed", "reason": "loudness measurement failed", "rel_path": rel_path}
        try:
            input_i = float(summary.get("input_i", "0"))
        except Exception:
            input_i = 0.0
        if input_i == float("-inf") or abs(input_i - _LOUDNORM_TARGET_I) <= _LOUDNORM_TOLERANCE_LU:
            return {"action": "skipped", "lufs": input_i, "rel_path": rel_path}

        if ext == ".flac":
            orig = FLAC(local_in)
            pictures, vc = orig.pictures, (dict(orig.tags) if orig.tags else {})
            codec_args = ["-c:a", "flac"]
        else:
            try:
                orig_id3 = ID3(local_in)
            except Exception:
                orig_id3 = None
            codec_args = ["-c:a", "libmp3lame", "-q:a", "0"]

        local_out = os.path.join(NORMALIZE_TMP_DIR, f"{uuid.uuid4().hex}_out{ext}")
        # True 2-pass loudnorm — feeds the measurement just taken back into
        # the actual render. The previous remote-only version measured
        # first but then ran a plain single-pass encode that re-measured
        # from scratch internally anyway, discarding the earlier pass
        # entirely; this is a real accuracy improvement that costs nothing
        # extra, since the measurement already happened above regardless.
        render_filter = (
            f"{LOUDNORM_FILTER}:measured_I={summary.get('input_i')}:"
            f"measured_TP={summary.get('input_tp')}:measured_LRA={summary.get('input_lra')}:"
            f"measured_thresh={summary.get('input_thresh')}:"
            f"offset={summary.get('target_offset', 0)}:linear=true"
        )
        cmd = (["ffmpeg", "-y", "-i", local_in, "-af", render_filter, "-map_metadata", "-1", "-vn"]
               + codec_args + [local_out])
        r = subprocess.run(_NICE + cmd, capture_output=True, text=True, timeout=280)
        if r.returncode != 0:
            return {"action": "failed", "reason": r.stderr[-300:], "rel_path": rel_path}

        if ext == ".flac":
            new_tags = FLAC(local_out)
            for k, v in vc.items():
                new_tags[k] = v
            for pic in pictures:
                new_tags.add_picture(pic)
            new_tags.save()
        elif orig_id3 is not None:
            orig_id3.save(local_out)

        remote_tmp = f"{remote_path}.normalizing.tmp"
        scp_up = ["scp", "-i", "/root/.ssh/id_rsa", "-P", str(ssh_cfg["port"]),
                  "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
                  local_out, f"{ssh_cfg['user']}@{ssh_cfg['host']}:{remote_tmp}"]
        r = subprocess.run(scp_up, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return {"action": "failed", "reason": f"upload failed: {r.stderr[-200:]}", "rel_path": rel_path}

        swap_script = ("import os; "
                        f"p={remote_path!r}; t={remote_tmp!r}; "
                        "os.chmod(t, os.stat(p).st_mode); os.replace(t, p)")
        swap_cmd = _ssh_cmd(ssh_cfg, f"python3 -c {shlex.quote(swap_script)}")
        r = subprocess.run(swap_cmd, capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            subprocess.run(_ssh_cmd(ssh_cfg, f"rm -f -- {shlex.quote(remote_tmp)}"),
                            capture_output=True, timeout=10)
            return {"action": "failed", "reason": f"remote swap failed: {r.stderr[-200:]}", "rel_path": rel_path}

        return {"action": "normalized", "lufs_before": input_i, "rel_path": rel_path}
    except Exception as e:
        return {"action": "failed", "reason": str(e)[:200], "rel_path": rel_path}
    finally:
        for f in (local_in, local_out):
            if f and os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

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

    # Clears out anything left behind by a previous run that crashed or got
    # killed mid-file — normal runs always clean up their own temp files.
    shutil.rmtree(NORMALIZE_TMP_DIR, ignore_errors=True)

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
    completed = 0
    # All tasks submitted up front — ThreadPoolExecutor's own internal queue
    # bounds how many actually run at once (NORMALIZE_WORKERS, matching this
    # container's CPU limit), so there's no need to hand-manage a submission
    # window. "Skip" cancels whatever's still queued (not-yet-started) rather
    # than one specific file — the at-most-NORMALIZE_WORKERS already running
    # just finish naturally; there's no single "current file" to interrupt
    # once several run concurrently.
    with ThreadPoolExecutor(max_workers=NORMALIZE_WORKERS) as executor:
        future_to_path = {executor.submit(_normalize_one_file_local, ssh_cfg, rel_path): rel_path
                           for rel_path in rel_paths}
        for future in as_completed(future_to_path):
            rel_path = future_to_path[future]
            completed += 1

            with job_lock:
                stop = jobs[job_id].get("skip_current", False)
                if stop:
                    jobs[job_id]["skip_current"] = False
            if stop:
                for f in future_to_path:
                    f.cancel()
                with job_lock:
                    jobs[job_id]["log"].append("⏭ Stopping — letting in-progress files finish, cancelling the rest")

            try:
                evt = future.result()
            except Exception as e:
                evt = {"action": "failed", "reason": str(e)[:200]}
            action = evt.get("action")

            if action == "normalized":
                normalized += 1
                lb = evt.get("lufs_before")
                lb_str = f"{lb:.1f} LUFS" if isinstance(lb, (int, float)) else "?"
                with job_lock:
                    jobs[job_id]["log"].append(f"🔊 Normalized: {rel_path} ({lb_str} → {_LOUDNORM_TARGET_I:.0f} LUFS)")
            elif action == "skipped":
                skipped += 1
            else:
                failed += 1
                with job_lock:
                    jobs[job_id]["log"].append(f"✗ Failed: {rel_path} — {evt.get('reason', 'unknown error')}")

            with job_lock:
                jobs[job_id]["current"] = completed
                jobs[job_id]["current_track"] = rel_path
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

# ─── Source Navidrome routes (browse + pull from someone else's server) ────

@app.route("/source-navidrome/config", methods=["GET"])
def source_nd_get_config():
    cfg = load_source_nd_config()
    if cfg:
        return jsonify({"configured": True, "url": cfg["url"], "user": cfg["user"]})
    return jsonify({"configured": False})

@app.route("/source-navidrome/config", methods=["POST"])
def source_nd_set_config():
    data = request.json or {}
    url, user, password = data.get("url","").strip(), data.get("user","").strip(), data.get("password","").strip()
    if not url or not user or not password:
        return jsonify({"error": "url, user and password required"}), 400
    try:
        nd_subsonic("ping", cfg={"url": url.rstrip("/"), "user": user, "password": password})
    except Exception as e:
        return jsonify({"error": f"Connection failed: {e}"}), 400
    save_source_nd_config(url, user, password)
    return jsonify({"status": "ok"})

@app.route("/source-navidrome/config", methods=["DELETE"])
def source_nd_delete_config():
    try:
        os.remove(SOURCE_NAVIDROME_CONFIG_FILE)
    except FileNotFoundError:
        pass
    return jsonify({"status": "ok"})

@app.route("/source-navidrome/cover/<cover_id>")
def source_nd_cover(cover_id):
    """Proxies cover art through our own backend rather than handing the
    frontend a direct <img src> URL with the source server's credentials
    embedded in it — those would otherwise go out over the wire (and into
    browser history/referrers) on every page load."""
    cfg = load_source_nd_config()
    if not cfg:
        return "", 404
    try:
        resp = http.get(f"{cfg['url']}/rest/getCoverArt", params={
            "id": cover_id, "u": cfg["user"], "p": cfg["password"], "v": "1.16.1", "c": "spotidrome"
        }, timeout=15)
        resp.raise_for_status()
        return Response(resp.content, mimetype=resp.headers.get("Content-Type", "image/jpeg"))
    except Exception:
        return "", 404

@app.route("/source-navidrome/albums", methods=["GET"])
def source_nd_albums():
    """Every album on the source server, alphabetical — paginates through
    the whole thing server-side (same idiom as cleanup_scan's song
    pagination) so the frontend gets one complete list in a single call."""
    cfg = load_source_nd_config()
    if not cfg:
        return jsonify({"error": "Not connected to a source Navidrome"}), 400
    try:
        albums = []
        offset = 0
        while True:
            data = nd_subsonic("getAlbumList2", cfg=cfg, type="alphabeticalByName", size=500, offset=offset)
            page = data.get("albumList2", {}).get("album", [])
            if not page:
                break
            albums.extend(page)
            if len(page) < 500:
                break
            offset += 500
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    return jsonify([{
        "id": a.get("id"), "name": a.get("name") or a.get("title", ""),
        "artist": a.get("artist", ""), "year": a.get("year"),
        "songCount": a.get("songCount", 0),
        "image": f"/source-navidrome/cover/{a['coverArt']}" if a.get("coverArt") else None,
    } for a in albums])

@app.route("/source-navidrome/albums/<album_id>/tracks", methods=["GET"])
def source_nd_album_tracks(album_id):
    cfg = load_source_nd_config()
    if not cfg:
        return jsonify({"error": "Not connected to a source Navidrome"}), 400
    try:
        data = nd_subsonic("getAlbum", cfg=cfg, id=album_id)
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    album = data.get("album", {})
    songs = album.get("song", [])
    return jsonify([{
        "id": s.get("id"), "name": s.get("title", ""), "artist": s.get("artist", ""),
        "album": s.get("album") or album.get("name", ""),
        "album_artist": album.get("artist") or s.get("artist", ""),
        "duration_ms": int((s.get("duration") or 0) * 1000),
        "track_number": s.get("track"), "suffix": s.get("suffix") or "flac",
        "image": f"/source-navidrome/cover/{s['coverArt']}" if s.get("coverArt") else None,
    } for s in songs])

def source_nd_download_worker(job_id, album_name, tracks, sync_navidrome):
    """Pulls each track's actual audio file straight from the source
    Navidrome (Subsonic 'download' — the original file, not a transcode)
    and rsyncs it into our own Navidrome exactly like a normal download
    batch. Deliberately does NOT touch any tags — unlike the YouTube path,
    these came from someone's real, presumably-already-correct library, so
    there's no fix_tags()/genre lookup step here, just a straight copy."""
    with job_lock:
        jobs[job_id]["status"] = "running"

    src_cfg = load_source_nd_config()
    ssh_cfg = load_ssh_config()
    nd_cfg = load_nd_config()
    if not src_cfg:
        with job_lock:
            jobs[job_id]["log"].append("✗ Not connected to a source Navidrome")
            jobs[job_id]["status"] = "done"
        return

    local_dir = os.path.join(DOWNLOAD_DIR, sanitize(album_name))
    os.makedirs(local_dir, exist_ok=True)
    downloaded_tracks = []

    for i, t in enumerate(tracks):
        with job_lock:
            if jobs[job_id].get("cancel_requested"):
                jobs[job_id]["log"].append(f"🛑 Cancelled ({i}/{len(tracks)})")
                jobs[job_id]["status"] = "done"
                jobs[job_id]["current_track"] = None
                save_jobs()
                return
            jobs[job_id]["current"] = i + 1
            jobs[job_id]["current_track"] = f"{t.get('artist','')} - {t.get('name','')}"

        song_id = t.get("id")
        label = f"{t.get('artist','')} - {t.get('name','')}"
        if not song_id:
            with job_lock:
                jobs[job_id]["log"].append(f"✗ Skipped (no source id): {label}")
                jobs[job_id]["failed"] += 1
            continue

        ext = (t.get("suffix") or "flac").lower()
        album = t.get("album") or album_name
        album_dir = os.path.join(local_dir, sanitize(album))
        os.makedirs(album_dir, exist_ok=True)
        out_path = os.path.join(album_dir, f"{sanitize(label)}.{ext}")

        try:
            resp = http.get(f"{src_cfg['url']}/rest/download", params={
                "id": song_id, "u": src_cfg["user"], "p": src_cfg["password"],
                "v": "1.16.1", "c": "spotidrome"}, timeout=60, stream=True)
            resp.raise_for_status()
            with open(out_path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=256 * 1024):
                    fh.write(chunk)
        except Exception as e:
            try:
                os.remove(out_path)
            except Exception:
                pass
            with job_lock:
                jobs[job_id]["log"].append(f"✗ Failed to pull {label}: {e}")
                jobs[job_id]["failed"] += 1
            continue

        downloaded_tracks.append(t)
        with job_lock:
            jobs[job_id]["log"].append(f"✓ Pulled: {label}")
            jobs[job_id]["downloaded"] += 1

    if downloaded_tracks and ssh_cfg:
        rsync_ok = rsync_to_remote(local_dir, ssh_cfg, job_id)
        if rsync_ok:
            shutil.rmtree(local_dir, ignore_errors=True)
            if nd_cfg:
                ok, msg = nd_trigger_scan(nd_cfg)
                with job_lock:
                    jobs[job_id]["log"].append(f"🔄 {msg}")
                if ok:
                    nd_wait_for_scan(nd_cfg, timeout=300)
                if sync_navidrome:
                    synced, missing = nd_sync_playlist(album_name, downloaded_tracks, nd_cfg, job_id)
                    with job_lock:
                        jobs[job_id]["nd_synced"] = synced
                        jobs[job_id]["nd_missing"] = missing

    with job_lock:
        jobs[job_id]["status"] = "done"
        jobs[job_id]["current_track"] = None
        save_jobs()

@app.route("/source-navidrome/download", methods=["POST"])
def source_nd_download():
    data = request.json or {}
    album_name = (data.get("album_name") or "Unknown Album").strip()
    tracks = data.get("tracks", [])
    sync_navidrome = data.get("sync_navidrome", True)
    if not tracks:
        return jsonify({"error": "No tracks provided"}), 400
    if not load_source_nd_config():
        return jsonify({"error": "Not connected to a source Navidrome"}), 400

    job_id = f"src_nd_{int(time.time()*1000)}"
    with job_lock:
        jobs[job_id] = {"id": job_id, "playlist": f"[Navidrome] {album_name}", "status": "pending",
                        "total": len(tracks), "current": 0, "downloaded": 0, "failed": 0,
                        "nd_synced": None, "nd_missing": None, "current_track": None, "log": []}
    threading.Thread(target=source_nd_download_worker,
                     args=(job_id, album_name, tracks, sync_navidrome), daemon=True).start()
    return jsonify({"job_id": job_id})

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

@app.route("/jobs/<job_id>/cancel", methods=["POST"])
def cancel_job(job_id):
    with job_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": "Not found"}), 404
        if job.get("status") == "done":
            return jsonify({"status": "already done"})
        job["cancel_requested"] = True
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
    # Optional (LunaDrome): library-only downloads - no Navidrome playlist, no
    # auto-sync entry - plus a known album name and a readable job label.
    sync_playlist = bool(data.get("sync_playlist", True))
    track_for_sync = bool(data.get("track_for_sync", True))
    album_hint = (data.get("album") or "").strip() or None
    album_artist_hint = (data.get("album_artist") or "").strip() or None
    # A whole album into one album folder (see ytmusic_download_worker's album_mode).
    complete_album = bool(data.get("complete_album", False))
    label = (data.get("job_label") or "").strip() or f"[YT] {playlist_name}"
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    job_id = f"yt_{int(time.time()*1000)}"
    with job_lock:
        jobs[job_id] = {"id": job_id, "playlist": label, "status": "pending",
                        "total": 0, "current": 0, "downloaded": 0, "failed": 0,
                        "nd_synced": None, "nd_missing": None, "current_track": None, "log": []}
    threading.Thread(target=ytmusic_download_worker,
                     args=(job_id, url, playlist_name, is_playlist),
                     kwargs={"sync_playlist": sync_playlist, "track_for_sync": track_for_sync,
                             "album_hint": album_hint, "complete_album": complete_album,
                             "album_artist_hint": album_artist_hint},
                     daemon=True).start()
    return jsonify({"job_id": job_id})


# ─── Search (LunaDrome "Download via SpotiDrome") ─────────────────────────────
# Interactive search for LunaDrome: YouTube Music songs + albums, then plain
# YouTube videos for covers/live versions. Ranking mirrors Jamidrome's search:
# YT Music's own "songs" category first (real releases, not reuploads/lyric
# videos), plain YouTube only filling in what's left, deduped.

def _yt_thumb(video_id):
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"

def _best_thumbnail(thumbnails):
    thumbs = thumbnails or []
    return thumbs[-1].get("url") if thumbs else None

def _ytm_call(fn, *args, timeout=12, **kwargs):
    """Run one ytmusicapi call under the shared lock with a hard timeout
    (ytmusicapi's HTTP calls have none of their own). None on any failure."""
    ytm = _get_ytmusic()
    if not ytm:
        return None
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        with _ytmusic_lock:  # serialize against every other caller of ytm — see _ytmusic_lock above
            return executor.submit(getattr(ytm, fn), *args, **kwargs).result(timeout=timeout)
    except Exception as e:
        print(f"[search] ytmusic {fn} failed: {e}", file=sys.stderr)
        return None
    finally:
        executor.shutdown(wait=False)

def _search_words(s):
    return {w for w in re.split(r"[^\w]+", (s or "").lower()) if len(w) > 2}

def lunadrome_search_songs(query, limit):
    results = _ytm_call("search", query, filter="songs", limit=limit) or []
    query_words = _search_words(query)
    out = []
    for r in results:
        video_id = r.get("videoId")
        if not video_id:
            continue
        artists = ", ".join(a.get("name", "") for a in (r.get("artists") or []) if a.get("name"))
        title = r.get("title") or "Unknown title"
        # YT Music matches loosely ("linkin park faint" also returns Numb): the
        # part of the query that isn't the artist's name has to relate to the
        # title - unless the query was just an artist name.
        leftover = query_words - _search_words(artists)
        if leftover and not (leftover & _search_words(title)):
            continue
        out.append({
            "video_id": video_id,
            "title": title,
            "artist": artists or "Unknown artist",
            "album": (r.get("album") or {}).get("name"),
            "duration": r.get("duration_seconds"),
            "thumbnail": _yt_thumb(video_id),
            "url": f"https://music.youtube.com/watch?v={video_id}",
            "kind": "song",
        })
    return out[:limit]

def lunadrome_search_videos(query, limit, exclude_ids):
    cmd = (["yt-dlp", "--dump-json", "--flat-playlist", "--no-playlist"] + YTDLP_POT_ARGS +
           [f"ytsearch{limit}:{query}"])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
    except (subprocess.TimeoutExpired, OSError):
        return []
    if result.returncode != 0:
        return []
    out = []
    for line in result.stdout.strip().split("\n"):
        try:
            e = json.loads(line) if line.strip() else None
        except Exception:
            e = None
        if not e or not e.get("id") or e.get("id") in exclude_ids:
            continue
        out.append({
            "video_id": e["id"],
            "title": e.get("title") or "Unknown title",
            "artist": re.sub(r" - Topic$", "", e.get("channel") or e.get("uploader") or "Unknown artist"),
            "album": None,
            "duration": e.get("duration"),
            "thumbnail": _best_thumbnail(e.get("thumbnails")) or _yt_thumb(e["id"]),
            "url": e.get("webpage_url") or f"https://www.youtube.com/watch?v={e['id']}",
            "kind": "video",
        })
    return out

def lunadrome_search_albums(query, limit):
    results = _ytm_call("search", query, filter="albums", limit=limit) or []
    out = []
    for r in results:
        browse_id = r.get("browseId")
        if not browse_id:
            continue
        artists = ", ".join(a.get("name", "") for a in (r.get("artists") or []) if a.get("name"))
        out.append({
            "browse_id": browse_id,
            "title": r.get("title") or "Unknown album",
            "artist": artists or "Unknown artist",
            "year": r.get("year"),
            "type": r.get("type") or "Album",
            "thumbnail": _best_thumbnail(r.get("thumbnails")),
        })
    return out[:limit]

@app.route("/search")
def lunadrome_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"songs": [], "albums": [], "videos": []})
    limit = max(1, min(int(request.args.get("limit", 12)), 30))
    songs = lunadrome_search_songs(q, limit)
    albums = lunadrome_search_albums(q, max(4, limit // 2))
    videos = lunadrome_search_videos(q, limit, {s["video_id"] for s in songs})
    return jsonify({"songs": songs, "albums": albums, "videos": videos})

@app.route("/search/album/<browse_id>")
def lunadrome_search_album(browse_id):
    album = _ytm_call("get_album", browse_id, timeout=15)
    if not album:
        return jsonify({"error": "Album not found"}), 404
    playlist_id = album.get("audioPlaylistId")
    artists = ", ".join(a.get("name", "") for a in (album.get("artists") or []) if a.get("name"))
    tracks = []
    for t in album.get("tracks") or []:
        tracks.append({
            "video_id": t.get("videoId"),
            "title": t.get("title") or "Unknown title",
            "artist": ", ".join(a.get("name", "") for a in (t.get("artists") or []) if a.get("name")) or artists,
            "duration": t.get("duration_seconds"),
            "track_number": t.get("trackNumber"),
        })
    return jsonify({
        "browse_id": browse_id,
        "title": album.get("title") or "Unknown album",
        "artist": artists or "Unknown artist",
        "year": album.get("year"),
        "thumbnail": _best_thumbnail(album.get("thumbnails")),
        "track_count": album.get("trackCount") or len(tracks),
        # What /ytmusic/download takes (is_playlist=true) to fetch the album.
        "url": f"https://music.youtube.com/playlist?list={playlist_id}" if playlist_id else None,
        "tracks": tracks,
    })

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


# ─── Edition consolidation (deluxe/remaster/etc. split across albums) ──────
# Spotify keeps a track's own "canonical album" pointed at whatever release
# it was originally on, even once a Deluxe/Remaster/Anniversary edition
# containing a copy of that same track exists too — so syncing a playlist
# faithfully tags each track by ITS OWN canonical album, which is accurate
# per-track but leaves something like a 22-track "Deluxe Version" release
# looking like a 7-track album in the library (just the tracks that only
# exist on the deluxe edition), with the other 15 sitting under the
# original release's name instead — never wrong exactly, just never
# assembled into the complete thing the way Spotify's own UI shows it.
# This scans the whole library for exactly that pattern (same artist, same
# base album name, more than one edition suffix in use) and completes
# EVERY edition actually present, copying a track in from a sibling
# edition wherever needed — never moving or deleting, since a Deluxe
# edition existing doesn't make the original release stop being a real,
# separate thing worth having intact too (2026-08-27: an earlier version
# of this moved tracks into just the single most-complete edition, which
# silently deleted the original release entirely once nothing was left
# under its name — fixed after exactly that happened to a real library).

_EDITION_SUFFIX_RE = re.compile(
    r"\s*[\(\[](deluxe(\s+version)?|super\s+deluxe|remaster(ed)?(\s*\d{0,4})?|"
    r"anniversary(\s+edition)?|special\s+edition|expanded(\s+edition)?|"
    r"bonus\s+track(s)?(\s+version)?)[\)\]]\s*$",
    re.IGNORECASE)

def _edition_base_name(album):
    return _EDITION_SUFFIX_RE.sub("", album or "").strip()

def find_edition_families(albums):
    """albums: [{'artist':..., 'name':...}, ...] (as returned by Navidrome).
    Returns {(artist, base_name): {raw_album_name, ...}} for every group
    where the same artist has more than one differently-named album
    sharing a base name once a trailing edition suffix is stripped —
    i.e. an actual candidate, not just every album in the library."""
    groups = {}
    for a in albums:
        artist, name = (a.get("artist") or "").strip(), (a.get("name") or "").strip()
        if not artist or not name:
            continue
        base = _edition_base_name(name)
        if not base:
            continue
        key = (artist, base)
        groups.setdefault(key, set()).add(name)
    return {k: v for k, v in groups.items() if len(v) > 1}

# A split caused by an *inconsistent album-artist tag* looks identical to a
# genuine multi-edition split in Navidrome's UI, but has nothing to do with
# edition suffixes — it's the same physical release, same folder, same
# "album" tag, just one or a few tracks carrying a different (or missing)
# ALBUMARTIST than the rest. Confirmed root cause of "some tracks of an
# album don't join the rest of it": a track's
# ALBUMARTIST tag was either never set (legacy download, predates fix_tags'
# `album_artist or artist` fallback) or, ironically, set *by a manual
# /failed/retry fix* that didn't pass through the album's real album_artist
# and fell back to that one track's own (collab) artist string instead.
#
# Empirically (checked directly against the live Navidrome library, not
# just inferred): an empty/missing ALBUMARTIST is harmless — Navidrome
# quietly folds it into whatever real value the rest of the folder has, no
# split. Two or more *different non-empty* values is what actually splits
# it. So detection only cares about distinct non-blank values, and the fix
# is conservative — realign the minority to the majority only when there's
# a real majority (>=3 tracks, >=60%) to anchor it on; a near-even split
# (often a genuine Various-Artists-style compilation, or two tracks with no
# way to tell which is "right") is left alone rather than guessed at.
_ALBUMARTIST_SCAN_REMOTE_SCRIPT = r'''
import json, os, sys
from mutagen.flac import FLAC
from mutagen.id3 import ID3

root = sys.argv[1]
out = {}
for entry in os.scandir(root):
    if not entry.is_dir():
        continue
    files_by_artist = {}
    for fn in os.listdir(entry.path):
        low = fn.lower()
        if not (low.endswith(".flac") or low.endswith(".mp3")):
            continue
        path = os.path.join(entry.path, fn)
        try:
            if low.endswith(".flac"):
                aa = (FLAC(path).get("albumartist") or [""])[0].strip()
            else:
                aa = str(ID3(path).get("TPE2", "")).strip()
        except Exception:
            continue
        if aa:
            files_by_artist.setdefault(aa, []).append(path)
    if len(files_by_artist) > 1:
        out[entry.name] = files_by_artist
print(json.dumps(out))
'''

_REALIGN_ALBUMARTIST_REMOTE_SCRIPT = r'''
import json, sys
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TPE2, error as ID3Error

fixes = json.loads(sys.argv[1])
results = []
for m in fixes:
    path = m["path"]
    try:
        if path.lower().endswith(".flac"):
            tags = FLAC(path)
            tags["albumartist"] = [m["album_artist"]]
            tags.save()
        elif path.lower().endswith(".mp3"):
            try:
                tags = ID3(path)
            except ID3Error:
                tags = ID3()
            tags["TPE2"] = TPE2(encoding=3, text=m["album_artist"])
            tags.save(path)
        else:
            results.append({"path": path, "ok": False, "error": "unsupported format"})
            continue
        results.append({"path": path, "ok": True})
    except Exception as e:
        results.append({"path": path, "ok": False, "error": str(e)[:200]})
print(json.dumps(results))
'''

ALBUMARTIST_SPLIT_MIN_TRACKS = 3
ALBUMARTIST_SPLIT_MIN_RATIO = 0.6

def scan_albumartist_splits(ssh_cfg):
    """Runs _ALBUMARTIST_SCAN_REMOTE_SCRIPT over the whole library in one
    SSH round-trip. Returns {folder_name: {artist: [paths]}} for every
    folder with 2+ distinct non-blank ALBUMARTIST values."""
    cmd = _ssh_cmd(ssh_cfg, f"python3 -c {shlex.quote(_ALBUMARTIST_SCAN_REMOTE_SCRIPT)} "
                             f"{shlex.quote(ssh_cfg['music_path'])}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if result.returncode != 0 or not result.stdout.strip():
        return {}
    try:
        return json.loads(result.stdout)
    except Exception:
        return {}

def find_albumartist_split_fixes(scan_result):
    """scan_result: output of scan_albumartist_splits. Returns
    {folder: {"winner": str, "paths": [path, ...]}} — only for folders with
    a confident majority worth realigning the minority to (see thresholds
    above); a near-even split is intentionally left out, not guessed at."""
    fixes = {}
    for folder, files_by_artist in scan_result.items():
        total = sum(len(paths) for paths in files_by_artist.values())
        winner, winner_paths = max(files_by_artist.items(), key=lambda kv: len(kv[1]))
        winner_n = len(winner_paths)
        if winner_n < ALBUMARTIST_SPLIT_MIN_TRACKS or winner_n / total < ALBUMARTIST_SPLIT_MIN_RATIO:
            continue
        outlier_paths = [p for artist, paths in files_by_artist.items()
                          if artist != winner for p in paths]
        if outlier_paths:
            fixes[folder] = {"winner": winner, "paths": outlier_paths}
    return fixes

def apply_albumartist_split_fixes(ssh_cfg, fixes):
    """fixes: output of find_albumartist_split_fixes. Retags every outlier
    file's ALBUMARTIST to its folder's winning value. Returns (ok_count,
    error_count)."""
    batch = [{"path": p, "album_artist": info["winner"]}
              for info in fixes.values() for p in info["paths"]]
    if not batch:
        return 0, 0
    cmd = _ssh_cmd(ssh_cfg, f"python3 -c {shlex.quote(_REALIGN_ALBUMARTIST_REMOTE_SCRIPT)} "
                             f"{shlex.quote(json.dumps(batch))}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    try:
        outcomes = json.loads(result.stdout.strip())
    except Exception:
        return 0, len(batch)
    ok = sum(1 for o in outcomes if o.get("ok"))
    return ok, len(outcomes) - ok

def _find_spotify_edition_tracklist(sp, artist, edition_name):
    """Looks up the real Spotify tracklist for THIS SPECIFIC edition name
    (not 'whichever edition is biggest' — every edition that's actually
    present in the library is a real, distinct release in its own right
    and deserves to be its own complete album, exactly as it exists on
    Spotify; a 'Deluxe' edition existing doesn't make the original
    release stop being a real thing worth having intact too). Returns
    (canonical_name, tracklist, cover_url) using Spotify's own name for
    the match (capitalization/punctuation can differ slightly from the
    library's tag) — None, None, None if nothing confidently matches.
    cover_url is this specific edition's own official artwork — every
    downloaded track only ever carries its individual source video's
    thumbnail as embedded art, never a real album cover, so without this
    Navidrome just shows whichever track it happens to scan first to
    represent the whole album (and two sibling editions can easily end
    up showing the exact same track's thumbnail as a result)."""
    try:
        result = sp.search(q=f'artist:"{artist}" album:"{edition_name}"', type="album", limit=5)
        candidates = result.get("albums", {}).get("items", [])
        if not candidates:
            result = sp.search(q=f"{artist} {edition_name}", type="album", limit=5)
            candidates = result.get("albums", {}).get("items", [])
        def score(c):
            return difflib.SequenceMatcher(None, c.get("name", "").lower(), edition_name.lower()).ratio()
        candidates = [c for c in candidates if score(c) >= 0.85]
        if not candidates:
            return None, None, None
        best = max(candidates, key=score)
        tracks = sp.album_tracks(best["id"])["items"]
        images = sorted(best.get("images") or [], key=lambda im: im.get("width") or 0, reverse=True)
        cover_url = images[0]["url"] if images else None
        return (best["name"],
                [{"title": t["name"], "duration_sec": t["duration_ms"] / 1000} for t in tracks],
                cover_url)
    except Exception as e:
        print(f"[editions] Spotify lookup failed for {artist} / {edition_name}: {e}", file=sys.stderr)
        return None, None, None

_APPLY_COVER_ART_REMOTE_SCRIPT = r'''
import base64, json, os, sys
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3, APIC, error as ID3Error

folder, mime = sys.argv[1], sys.argv[2]
img_data = base64.b64decode(sys.stdin.read())

updated, failed = 0, []
for fn in os.listdir(folder):
    p = os.path.join(folder, fn)
    ext = os.path.splitext(fn)[1].lower()
    try:
        if ext == ".flac":
            tags = FLAC(p)
            tags.clear_pictures()
            pic = Picture()
            pic.data = img_data
            pic.type = 3  # front cover
            pic.mime = mime
            tags.add_picture(pic)
            tags.save()
            updated += 1
        elif ext == ".mp3":
            try:
                tags = ID3(p)
            except ID3Error:
                tags = ID3()
            tags.delall("APIC")
            tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=img_data))
            tags.save(p)
            updated += 1
    except Exception as e:
        failed.append({"file": fn, "error": str(e)[:150]})
print(json.dumps({"updated": updated, "failed": failed}))
'''

def _apply_cover_art(ssh_cfg, album_folder_name, cover_url):
    """Downloads cover_url (this container has internet access; the
    Navidrome host doesn't necessarily) and embeds it as the front-cover
    picture on every track in album_folder_name, replacing whatever
    per-track art is already there. Returns (updated_count, [errors])."""
    if not cover_url:
        return 0, ["no cover art available from Spotify for this edition"]
    try:
        resp = http.get(cover_url, timeout=15)
        resp.raise_for_status()
        img_data = resp.content
        mime = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip() or "image/jpeg"
    except Exception as e:
        return 0, [f"cover art download failed: {e}"]

    folder_path = f"{ssh_cfg['music_path']}/{sanitize(album_folder_name)}"
    cmd = _ssh_cmd(ssh_cfg, f"python3 -c {shlex.quote(_APPLY_COVER_ART_REMOTE_SCRIPT)} "
                             f"{shlex.quote(folder_path)} {shlex.quote(mime)}")
    try:
        result = subprocess.run(cmd, input=base64.b64encode(img_data).decode(),
                                 capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return 0, [result.stderr[-300:]]
        outcome = json.loads(result.stdout.strip() or "{}")
        return outcome.get("updated", 0), outcome.get("failed", [])
    except Exception as e:
        return 0, [str(e)]

def consolidate_editions_worker(job_id):
    with job_lock:
        jobs[job_id]["status"] = "running"

    ssh_cfg = load_ssh_config()
    nd_cfg = load_nd_config()
    if not ssh_cfg or not nd_cfg:
        with job_lock:
            jobs[job_id]["log"].append("✗ SSH and/or Navidrome not configured")
            jobs[job_id]["status"] = "done"
        return
    sp, auth_url = get_sp()
    if not sp:
        with job_lock:
            jobs[job_id]["log"].append("✗ Spotify not authenticated — can't tell which edition is the complete one")
            jobs[job_id]["status"] = "done"
        return

    with job_lock:
        jobs[job_id]["current_track"] = "Clearing stale album entries first…"
    try:
        prune_result = prune_orphaned_navidrome_entries(ssh_cfg, nd_cfg)
        if prune_result.get("pruned_albums") or prune_result.get("pruned"):
            with job_lock:
                jobs[job_id]["log"].append(
                    f"🧹 Cleared {prune_result.get('pruned_albums', 0)} stale album entries "
                    f"and {prune_result.get('pruned', 0)} stale track entries first — these would "
                    f"have muddied the picture below otherwise")
    except Exception as e:
        print(f"[editions] Pre-scan prune failed: {e}", file=sys.stderr)

    with job_lock:
        jobs[job_id]["current_track"] = "Fetching the library from Navidrome…"
    all_songs, all_albums, offset = [], [], 0
    while True:
        data = nd_subsonic("search3", cfg=nd_cfg, query="", songCount=500, songOffset=offset,
                            albumCount=500, albumOffset=offset, artistCount=0)
        result = data.get("searchResult3", {})
        songs, albums = result.get("song", []), result.get("album", [])
        if not songs and not albums:
            break
        all_songs.extend(songs)
        all_albums.extend(albums)
        if len(songs) < 500 and len(albums) < 500:
            break
        offset += 500

    families = find_edition_families(all_albums)
    with job_lock:
        jobs[job_id]["total"] = len(families)
        jobs[job_id]["log"].append(
            f"ℹ Found {len(all_albums)} album(s), {len(families)} look like a split edition")

    if not families:
        with job_lock:
            jobs[job_id]["log"].append("✅ Nothing looks split across editions — library already consolidated")
            jobs[job_id]["status"] = "done"
            save_jobs()
        return

    songs_by_artist = {}
    for s in all_songs:
        songs_by_artist.setdefault((s.get("artist") or "").strip(), []).append(s)

    consolidated_total = missing_total = unresolved = 0
    touched_any = False
    for i, ((artist, base), raw_names) in enumerate(sorted(families.items())):
        with job_lock:
            jobs[job_id]["current"] = i + 1
            jobs[job_id]["current_track"] = f"{artist} — {base}"
            skip = jobs[job_id].get("skip_current", False)
            if skip:
                jobs[job_id]["skip_current"] = False
        if skip:
            with job_lock:
                jobs[job_id]["log"].append(f"⏭ Skipped: {artist} — {base}")
            continue

        candidates = songs_by_artist.get(artist, [])
        # Every edition actually present in the library is a real,
        # separate release worth having complete in its own right — a
        # Deluxe edition existing doesn't make the original release stop
        # being a real thing. So this completes EACH edition found here,
        # copying a track in from a sibling edition when needed rather
        # than moving it — the original never loses tracks just because
        # they're also needed somewhere else.
        for edition_name in sorted(raw_names):
            canonical_name, tracklist, cover_url = _find_spotify_edition_tracklist(sp, artist, edition_name)
            if not tracklist:
                with job_lock:
                    jobs[job_id]["log"].append(f"? {artist} — {edition_name}: no confident Spotify match, leaving as-is")
                unresolved += 1
                continue

            copies, missing = [], []
            for track in tracklist:
                # Already sitting correctly under this edition? Nothing to do.
                if any(s.get("album") == canonical_name and _title_ok(s.get("title", ""), track["title"])
                       for s in candidates):
                    continue
                # Otherwise, a copy under some sibling edition that can be
                # copied in — duration has to agree too, since two
                # different tracks can share a title.
                source = next((s for s in candidates
                               if s.get("album") != canonical_name
                               and _title_ok(s.get("title", ""), track["title"])
                               and _duration_close(s.get("duration") or 0, track["duration_sec"])), None)
                if source:
                    copies.append({"song": source, "title": track["title"]})
                else:
                    missing.append(track["title"])

            if not copies and not missing:
                with job_lock:
                    jobs[job_id]["log"].append(f"✓ {artist} — {canonical_name}: already complete")

            if copies:
                ids = [c["song"]["id"] for c in copies]
                id_list = ",".join(f"'{i}'" for i in ids)
                select_cmd = _ssh_cmd(ssh_cfg,
                    f"sqlite3 /var/lib/navidrome/navidrome.db "
                    f"\"SELECT id || char(1) || path FROM media_file WHERE id IN ({id_list});\"")
                result = subprocess.run(select_cmd, capture_output=True, text=True, timeout=30)
                real_paths = {}
                for line in result.stdout.splitlines():
                    if "\x01" not in line:
                        continue
                    rid, path = line.split("\x01", 1)
                    real_paths[rid] = path

                new_dir = f"{ssh_cfg['music_path']}/{sanitize(canonical_name)}"
                batch = []
                for c in copies:
                    real_path = real_paths.get(c["song"]["id"])
                    if not real_path:
                        continue
                    old_full = real_path if real_path.startswith("/") else f"{ssh_cfg['music_path']}/{real_path}"
                    batch.append({"old": old_full, "new_dir": new_dir, "new_album": canonical_name})

                if batch:
                    script_cmd = _ssh_cmd(ssh_cfg,
                        f"python3 -c {shlex.quote(_CONSOLIDATE_REMOTE_SCRIPT)} {shlex.quote(json.dumps(batch))}")
                    result = subprocess.run(script_cmd, capture_output=True, text=True, timeout=120)
                    try:
                        outcomes = json.loads(result.stdout.strip())
                    except Exception:
                        outcomes = []
                    ok_count = sum(1 for o in outcomes if o.get("ok"))
                    consolidated_total += ok_count
                    touched_any = touched_any or ok_count > 0
                    with job_lock:
                        jobs[job_id]["log"].append(
                            f"🔀 {artist} — {canonical_name}: copied {ok_count}/{len(batch)} track(s) in "
                            f"from elsewhere in the library (originals left in place)")

            if missing:
                missing_total += len(missing)
                with job_lock:
                    jobs[job_id]["log"].append(
                        f"⚠ {artist} — {canonical_name}: {len(missing)} track(s) not found anywhere in the "
                        f"library at all (needs a fresh download, not just a retag): {', '.join(missing[:5])}"
                        f"{'…' if len(missing) > 5 else ''}")

            # Every downloaded track only ever carries its own individual
            # source video's thumbnail as embedded art, never a real album
            # cover — so this runs regardless of whether the edition needed
            # any tracks copied in, fixing wrong/inconsistent art even on
            # an edition that was already complete.
            art_updated, art_errors = _apply_cover_art(ssh_cfg, canonical_name, cover_url)
            if art_updated:
                touched_any = True
                with job_lock:
                    jobs[job_id]["log"].append(
                        f"🖼 {artist} — {canonical_name}: set the correct cover art on {art_updated} track(s)")
            elif art_errors:
                with job_lock:
                    jobs[job_id]["log"].append(
                        f"⚠ {artist} — {canonical_name}: couldn't set cover art ({art_errors[0]})")

            with job_lock:
                jobs[job_id]["downloaded"] = consolidated_total
                jobs[job_id]["failed"] = missing_total

    if touched_any:
        with job_lock:
            jobs[job_id]["current_track"] = "Cleaning up now-orphaned entries and rescanning…"
        try:
            prune_orphaned_navidrome_entries(ssh_cfg, nd_cfg)
        except Exception as e:
            with job_lock:
                jobs[job_id]["log"].append(f"⚠ Orphan cleanup failed: {e}")

    with job_lock:
        jobs[job_id]["log"].append(
            f"✅ Done: {consolidated_total} track(s) consolidated, {missing_total} missing entirely, "
            f"{unresolved} famil{'y' if unresolved == 1 else 'ies'} had no confident Spotify match")
        jobs[job_id]["status"] = "done"
        jobs[job_id]["current_track"] = None
        save_jobs()

_CONSOLIDATE_REMOTE_SCRIPT = r'''
import json, os, shutil, sys
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TALB, error as ID3Error

# COPY, never move or retag in place — the source file (and whichever
# edition it already belongs to) is left completely untouched. A track
# needed on more than one real edition genuinely exists on both, exactly
# as it does on Spotify itself; consolidating one edition must never come
# at the cost of another edition losing a track it's also supposed to have.
copies = json.loads(sys.argv[1])
results = []
for m in copies:
    old = m["old"]
    try:
        ext = os.path.splitext(old)[1].lower()
        if ext not in (".flac", ".mp3"):
            results.append({"old": old, "ok": False, "error": "unsupported format"})
            continue
        os.makedirs(m["new_dir"], exist_ok=True)
        new_path = os.path.join(m["new_dir"], os.path.basename(old))
        if os.path.abspath(old) == os.path.abspath(new_path):
            results.append({"old": old, "ok": False, "error": "source and destination are the same file"})
            continue
        # copy2 preserves permission bits from the source (unlike a fresh
        # write, which would pick up whatever this process's own umask
        # defaults to) — the exact same lesson already learned the hard
        # way with tempfile.mkstemp() defaulting to 0600 elsewhere in this
        # app; the source here is already correctly-permissioned, so this
        # carries that forward rather than risking a fresh default.
        shutil.copy2(old, new_path)
        if ext == ".flac":
            tags = FLAC(new_path)
            tags["album"] = [m["new_album"]]
            tags.save()
        else:
            try:
                tags = ID3(new_path)
            except ID3Error:
                tags = ID3()
            tags["TALB"] = TALB(encoding=3, text=m["new_album"])
            tags.save(new_path)
        results.append({"old": old, "new": new_path, "ok": True})
    except Exception as e:
        results.append({"old": old, "ok": False, "error": str(e)[:200]})
print(json.dumps(results))
'''


@app.route("/library/consolidate-editions", methods=["POST"])
def library_consolidate_editions():
    ssh_cfg = load_ssh_config()
    if not ssh_cfg:
        return jsonify({"error": "SSH not configured"}), 400
    try:
        sp, url = get_sp()
    except Exception:
        sp, url = None, None
    if not sp:
        return jsonify({"error": "Spotify not authenticated", "auth_url": url}), 401

    job_id = f"consolidate_editions_{int(time.time()*1000)}"
    with job_lock:
        jobs[job_id] = {"id": job_id, "playlist": "[Consolidate Editions] Library", "status": "pending",
                        "total": 0, "current": 0, "downloaded": 0, "failed": 0,
                        "nd_synced": None, "nd_missing": None, "current_track": None, "log": []}
    threading.Thread(target=consolidate_editions_worker, args=(job_id,), daemon=True).start()
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


def _delete_tracks_from_navidrome(track_ids, ssh_cfg, cfg, delete_files=True):
    """Delete tracks from Navidrome's DB and (optionally) the underlying
    files on the remote host — the real DELETE FROM media_file, not just a
    file removal that Navidrome would otherwise re-flag as merely
    'missing' on its next scan (see find_orphaned_navidrome_entries),
    which is what leaves a blank/ghost entry behind instead of removing
    the track outright.

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
    trusting the API's convenience field.

    Shared by /cleanup/delete and /library/long-tracks/remove — returns
    (deleted_paths, failed_reasons)."""
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

    return deleted, failed


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

    deleted, failed = _delete_tracks_from_navidrome(track_ids, ssh_cfg, cfg, delete_files)

    return jsonify({
        "deleted_files": len(deleted),
        "failed": len(failed),
        "failed_list": failed[:10],
    })


@app.route("/library/long-tracks", methods=["GET"])
def library_long_tracks():
    """Scan the whole Navidrome library for tracks longer than
    LONG_TRACK_THRESHOLD_SEC (15 min) — these are usually a Let's Play
    episode, a full album/OST rip, or a DJ mix that slipped past the
    download-time sanity checks (see spotidrome-wrong-track-bugs) — PLUS
    tracks Navidrome lists at exactly 0:00. A real audio file is never
    genuinely zero-length; a 0 duration means Navidrome's own scanner
    failed to read the file's duration at all (a truncated/corrupt
    download, or a tag it couldn't parse), which is just as worth a look
    as an implausibly long one — same review page, same whitelist/remove
    actions, just a different "reason" this file got flagged. Skips
    anything already whitelisted."""
    cfg = load_nd_config()
    if not cfg:
        return jsonify({"error": "Navidrome not configured"}), 400

    whitelist = load_long_track_whitelist()

    try:
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

        long_tracks = []
        for s in all_songs:
            duration = s.get("duration", 0) or 0
            if duration == 0:
                reason = "zero_duration"
            elif duration > LONG_TRACK_THRESHOLD_SEC:
                reason = "long"
            else:
                continue
            if track_ignore_key(s.get("artist"), s.get("title")) in whitelist:
                continue
            long_tracks.append({
                "id": s["id"],
                "title": s.get("title", ""),
                "artist": s.get("artist", ""),
                "album": s.get("album", ""),
                "album_artist": s.get("albumArtist") or s.get("artist", ""),
                "duration": duration,
                "reason": reason,
                "bitRate": s.get("bitRate", 0),
                "path": s.get("path", ""),
            })
        # Longest first, then zero-duration entries grouped at the end.
        long_tracks.sort(key=lambda t: t["duration"], reverse=True)

        return jsonify({
            "total_scanned": len(all_songs),
            "threshold_sec": LONG_TRACK_THRESHOLD_SEC,
            "long_tracks": long_tracks,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/library/long-tracks/whitelist", methods=["POST"])
def library_long_tracks_whitelist():
    """Mark a track as a legitimately long one — it stops showing up in
    /library/long-tracks but is otherwise left completely alone (still
    playable, still re-downloadable, nothing deleted or blocked)."""
    data = request.json or {}
    artist, title = data.get("artist", ""), data.get("title", "")
    if not artist or not title:
        return jsonify({"error": "artist and title required"}), 400
    key = track_ignore_key(artist, title)
    whitelist = load_long_track_whitelist()
    whitelist[key] = {"artist": artist, "title": title, "added_at": datetime.utcnow().isoformat()}
    save_long_track_whitelist(whitelist)
    return jsonify({"status": "ok"})


@app.route("/library/long-tracks/remove", methods=["POST"])
def library_long_tracks_remove():
    """Delete a long track from Navidrome entirely — file on disk plus its
    media_file row, so no blank/ghost entry is left behind (same delete
    path as /cleanup/delete) — and add it to the permanent-ignore list so
    it's never re-downloaded by a future sync or manual retry."""
    data = request.json or {}
    track_id = data.get("id", "")
    artist, title = data.get("artist", ""), data.get("title", "")
    if not track_id or not artist or not title:
        return jsonify({"error": "id, artist and title required"}), 400

    cfg = load_nd_config()
    ssh_cfg = load_ssh_config()
    if not cfg:
        return jsonify({"error": "Navidrome not configured"}), 400

    deleted, failed = _delete_tracks_from_navidrome([track_id], ssh_cfg, cfg, delete_files=True)

    key = track_ignore_key(artist, title)
    ignored = load_ignored_tracks()
    ignored[key] = {
        "artist": artist, "title": title,
        "added_at": datetime.utcnow().isoformat(),
        "reason": "Removed via /library/long-tracks (over 15 min, or listed at 0:00)",
    }
    save_ignored_tracks(ignored)

    return jsonify({
        "status": "ok",
        "deleted_files": len(deleted),
        "failed": failed,
    })


# ─── Track match verification ──────────────────────────────────────────────
# fix_tags() overwrites a downloaded file's own title/artist tags with the
# *expected* metadata right after download — so once a wrong match slips
# through (see spotidrome-wrong-track-bugs), the library file itself no
# longer carries any trace of what it actually is. The one surviving clue
# is the "comment" tag, which fix_tags() sets to the actual source URL. This
# re-resolves that URL via yt-dlp and diffs the *real* video's title/artist
# against the library tag — the same checks _downloaded_file_looks_wrong()
# already runs at download time, just run again after the fact so it also
# catches tracks that were downloaded before that safety net existed.

TRACK_VERIFY_STATE_FILE       = "/root/.ssh/track_verify_state.json"
TRACK_MISMATCH_REPORT_FILE    = "/root/.ssh/track_mismatch_report.json"
TRACK_MISMATCH_WHITELIST_FILE = "/root/.ssh/track_mismatch_whitelist.json"
# Each check is a real yt-dlp network round-trip (a few seconds each) —
# capped per run for the same reason scan_and_dedupe_by_title caps itself
# (MAX_TITLE_DEDUPE_PER_RUN): keep one run's wall-clock predictable rather
# than one job blocking on the entire library. Re-run to keep going —
# already-verified tracks are cached and skipped on the next run.
MAX_VERIFY_CHECKS_PER_RUN = 300
# Bump this whenever _verify_track_against_source's rules change. The cache
# below is keyed on (path, comment) alone, which only tells you the *file*
# hasn't changed — not that it was checked with today's logic. Without this,
# a rule fix (like the channel/uploader false-positive fix below) would
# silently keep serving verdicts computed under the old, wrong rules for
# every track already cached, since its file and comment URL didn't change.
TRACK_VERIFY_LOGIC_VERSION = 3  # v3: an unresolvable lookup used to get silently cached as "ok" forever — now it isn't cached at all, see verify_tracks_worker

def load_track_verify_state():
    try:
        with open(TRACK_VERIFY_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_track_verify_state(data):
    with open(TRACK_VERIFY_STATE_FILE, "w") as f:
        json.dump(data, f, indent=2)

def load_track_mismatch_report():
    try:
        with open(TRACK_MISMATCH_REPORT_FILE) as f:
            return json.load(f)
    except Exception:
        return {"last_run": None, "total_tracks": 0, "checked_this_run": 0,
                 "still_unverified": 0, "mismatches": []}

def save_track_mismatch_report(data):
    with open(TRACK_MISMATCH_REPORT_FILE, "w") as f:
        json.dump(data, f, indent=2)

def load_track_mismatch_whitelist():
    try:
        with open(TRACK_MISMATCH_WHITELIST_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_track_mismatch_whitelist(data):
    with open(TRACK_MISMATCH_WHITELIST_FILE, "w") as f:
        json.dump(data, f, indent=2)

# One SSH round-trip reads every track's tags at once — same idiom as
# _GENRE_SCAN_REMOTE_SCRIPT / _TITLE_DEDUPE_SCAN_SCRIPT, just pulling
# "comment" (the source URL) and album_artist as well.
_VERIFY_SCAN_SCRIPT = r'''
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
                album_artist = (tags.get("albumartist") or [""])[0]
                comment = (tags.get("comment") or [""])[0]
                duration = tags.info.length if tags.info else 0
            elif f.endswith(".mp3"):
                tags = ID3(p)
                title = str(tags.get("TIT2", ""))
                artist = str(tags.get("TPE1", ""))
                album_artist = str(tags.get("TPE2", ""))
                comm = tags.getall("COMM")
                comment = str(comm[0]) if comm else ""
                duration = mutagen.File(p).info.length
            else:
                continue
        except Exception:
            continue
        if title and artist:
            out.append({"path": p, "title": title, "artist": artist,
                        "album_artist": album_artist, "comment": comment,
                        "duration": duration or 0,
                        "album": os.path.basename(os.path.dirname(p))})
print(json.dumps(out))
'''

def _lookup_source_identity(url, timeout=20):
    """Ask yt-dlp what a source URL actually is (title/artist/description),
    without downloading it — mirrors lookup_real_album()'s pattern."""
    if not url:
        return None
    try:
        cmd = ["yt-dlp", "--dump-json", "--no-playlist", "--skip-download",
               "--socket-timeout", "10", url]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return json.loads(result.stdout.strip().split("\n")[0])
    except Exception:
        return None

def _verify_track_against_source(title, artist, comment_url):
    """Returns (status, reason). status is 'ok' (verified, no mismatch),
    'mismatch' (verified, and it's wrong — reason set), or 'unresolvable'
    (has a source URL but couldn't resolve it right now — dead link,
    network hiccup, rate limit, etc.). Callers must NOT cache
    'unresolvable' as a permanent verdict, unlike the other two — a
    transient failure today says nothing about whether it'd resolve fine
    on a later run, and caching it as settled would silently stop this
    track from ever being re-checked.

    Deliberately mirrors _downloaded_file_looks_wrong()'s exact signals —
    not looser ones — just fed freshly-refetched source metadata instead of
    the originally-embedded tags fix_tags() already overwrote on disk. In
    particular: only yt-dlp's own dedicated 'artist' field counts as ground
    truth for the artist check, same as that function only trusts the
    file's --add-metadata-embedded artist tag — NOT a channel/uploader
    name, which is an upload *account*, not a performer credit, and falling
    back to it produced real false positives in testing (a legitimate,
    correctly-tagged song re-uploaded by some unrelated compilation/fan
    channel still isn't 'by' that channel). Absent is not evidence of
    anything either way — only a confident mismatch rejects."""
    info = _lookup_source_identity(comment_url)
    if not info:
        return "unresolvable", None

    real_title = info.get("track") or info.get("title") or ""
    real_artist = (info.get("artist") or "").strip()
    real_desc = info.get("description") or ""

    if _looks_like_non_music(f"{real_title} {real_desc}", title):
        return "mismatch", (f"the source video's own title/description looks like non-music content "
                             f"(podcast, let's play, walkthrough, full compilation, etc.), not '{title}'")
    if real_artist and not _artist_ok([real_artist], artist):
        return "mismatch", (f"the source video's own artist tag is '{real_artist}', not '{artist}' — "
                             f"almost certainly a different artist's same-titled song")
    return "ok", None

def verify_tracks_worker(job_id):
    with job_lock:
        jobs[job_id]["status"] = "running"

    ssh_cfg = load_ssh_config()
    if not ssh_cfg:
        with job_lock:
            jobs[job_id]["log"].append("✗ SSH not configured")
            jobs[job_id]["status"] = "done"
        return

    with job_lock:
        jobs[job_id]["current_track"] = "Reading tags for every track on the Navidrome host…"
    scan_cmd = _ssh_cmd(ssh_cfg, f"python3 -c {shlex.quote(_VERIFY_SCAN_SCRIPT)} {shlex.quote(ssh_cfg['music_path'])}")
    try:
        result = _run_remote_scan(scan_cmd, timeout=180)
        if result.returncode != 0:
            raise ValueError(result.stderr[-500:])
        files = json.loads(result.stdout.strip() or "[]")
    except Exception as e:
        with job_lock:
            jobs[job_id]["log"].append(f"✗ Failed to scan library: {e}")
            jobs[job_id]["status"] = "done"
        return

    whitelist = load_track_mismatch_whitelist()
    state = load_track_verify_state()

    with job_lock:
        jobs[job_id]["total"] = len(files)
        jobs[job_id]["log"].append(f"📚 {len(files)} track(s) on disk — verifying each against its recorded source…")

    mismatches = []
    checked_this_run = 0
    still_unverified = 0

    for i, f in enumerate(files):
        with job_lock:
            if jobs[job_id].get("cancel_requested"):
                jobs[job_id]["log"].append(f"🛑 Cancelled ({i}/{len(files)} scanned)")
                jobs[job_id]["status"] = "done"
                jobs[job_id]["current_track"] = None
                save_jobs()
                return
            jobs[job_id]["current"] = i + 1
            jobs[job_id]["current_track"] = f"{f['artist']} - {f['title']}"

        if track_ignore_key(f["artist"], f["title"]) in whitelist:
            continue

        cached = state.get(f["path"])
        if (cached and cached.get("comment") == f.get("comment")
                and cached.get("logic_version") == TRACK_VERIFY_LOGIC_VERSION):
            # Already checked this exact file against this exact source
            # under today's rules — nothing has changed since, so don't
            # spend another yt-dlp round-trip re-confirming the same answer.
            if cached.get("verdict") == "mismatch" and cached.get("entry"):
                mismatches.append(cached["entry"])
            continue

        if not f.get("comment"):
            # No source URL recorded at all (e.g. the Subspace Sequence
            # case) — nothing to check, and that's a stable fact about the
            # file, not a network call, so it's free: cache it and move on
            # without touching this run's yt-dlp-call budget.
            state[f["path"]] = {"comment": None, "verdict": "ok",
                                 "logic_version": TRACK_VERIFY_LOGIC_VERSION,
                                 "checked_at": datetime.utcnow().isoformat()}
            continue

        if checked_this_run >= MAX_VERIFY_CHECKS_PER_RUN:
            still_unverified += 1
            continue

        status, reason = _verify_track_against_source(f["title"], f["artist"], f["comment"])
        checked_this_run += 1

        if status == "unresolvable":
            # Don't cache — a dead/rate-limited/flaky lookup today doesn't
            # mean anything about tomorrow, and caching it as settled would
            # silently stop this track from ever being re-checked.
            continue
        elif status == "mismatch":
            entry = {
                "path": f["path"], "title": f["title"], "artist": f["artist"],
                "album": f["album"], "album_artist": f.get("album_artist") or f["artist"],
                "duration_ms": int((f.get("duration") or 0) * 1000),
                "comment": f.get("comment"), "reason": reason,
            }
            state[f["path"]] = {"comment": f.get("comment"), "verdict": "mismatch", "entry": entry,
                                 "logic_version": TRACK_VERIFY_LOGIC_VERSION,
                                 "checked_at": datetime.utcnow().isoformat()}
            mismatches.append(entry)
            with job_lock:
                jobs[job_id]["log"].append(f"⚠ {f['artist']} - {f['title']}: {reason}")
        else:  # ok
            state[f["path"]] = {"comment": f.get("comment"), "verdict": "ok",
                                 "logic_version": TRACK_VERIFY_LOGIC_VERSION,
                                 "checked_at": datetime.utcnow().isoformat()}

        if checked_this_run % 20 == 0:
            save_track_verify_state(state)  # checkpoint periodically so a crash mid-run doesn't lose progress

    save_track_verify_state(state)
    save_track_mismatch_report({
        "last_run": datetime.utcnow().isoformat(),
        "total_tracks": len(files),
        "checked_this_run": checked_this_run,
        "still_unverified": still_unverified,
        "mismatches": mismatches,
    })

    with job_lock:
        jobs[job_id]["status"] = "done"
        jobs[job_id]["current_track"] = None
        jobs[job_id]["downloaded"] = len(mismatches)
        msg = f"✅ Verified {checked_this_run} track(s) this run — {len(mismatches)} mismatch(es) on record."
        if still_unverified:
            msg += f" {still_unverified} track(s) still unchecked — run again to continue."
        jobs[job_id]["log"].append(msg)

def _sql_quote(s):
    """Escape a value for embedding as a SQLite string literal (doubling
    embedded single quotes) — unlike the Navidrome-generated ids used
    elsewhere in this file, a raw filesystem path can legitimately contain
    a quote (e.g. \"King Bowser's Might\"), so it can't be interpolated
    unescaped into SQL text."""
    return "'" + str(s).replace("'", "''") + "'"

def _delete_track_by_path(path, ssh_cfg, cfg):
    """Delete one track from Navidrome by its literal on-disk path — used
    where a raw filesystem/tag scan already found the exact file (so there's
    no Subsonic song id to key off, unlike _delete_tracks_from_navidrome).
    Same file-then-DB-row delete plus full rescan."""
    if not ssh_cfg:
        return False, "SSH not configured"
    check_cmd = _ssh_cmd(ssh_cfg, f"test -f {shlex.quote(path)} && echo yes || echo no")
    exists = subprocess.run(check_cmd, capture_output=True, text=True, timeout=10).stdout.strip() == "yes"
    if exists:
        rm_cmd = _ssh_cmd(ssh_cfg, f"rm -f -- {shlex.quote(path)}")
        result = subprocess.run(rm_cmd, capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            return False, f"rm failed: {result.stderr.strip()}"
    sql = f"DELETE FROM media_file WHERE path = {_sql_quote(path)};"
    cmd = _ssh_cmd(ssh_cfg, f"sqlite3 /var/lib/navidrome/navidrome.db {shlex.quote(sql)}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        return False, f"DB delete failed: {result.stderr.strip()}"
    if cfg:
        ok, _ = nd_trigger_scan(cfg, full=True)
        if ok:
            nd_wait_for_scan(cfg, timeout=300)
    return True, None

@app.route("/library/verify-tracks", methods=["POST"])
def library_verify_tracks():
    """Kick off a background scan that re-checks every track's tags against
    its own recorded source (see the section comment above)."""
    ssh_cfg = load_ssh_config()
    if not ssh_cfg:
        return jsonify({"error": "SSH not configured"}), 400
    job_id = f"verify_tracks_{int(time.time()*1000)}"
    with job_lock:
        jobs[job_id] = {"id": job_id, "playlist": "[Verify] Library", "status": "pending",
                        "total": 0, "current": 0, "downloaded": 0, "failed": 0,
                        "nd_synced": None, "nd_missing": None, "current_track": None, "log": []}
    threading.Thread(target=verify_tracks_worker, args=(job_id,), daemon=True).start()
    return jsonify({"job_id": job_id})

@app.route("/library/mismatched-tracks", methods=["GET"])
def library_mismatched_tracks():
    """Return the most recent verify scan's results — doesn't re-scan
    (that's what /library/verify-tracks is for), just serves the saved
    report, filtered against whatever's been whitelisted since."""
    report = load_track_mismatch_report()
    whitelist = load_track_mismatch_whitelist()
    mismatches = [m for m in report.get("mismatches", [])
                  if track_ignore_key(m.get("artist"), m.get("title")) not in whitelist]
    return jsonify({**report, "mismatches": mismatches})

@app.route("/library/mismatched-tracks/whitelist", methods=["POST"])
def library_mismatched_tracks_whitelist():
    """Mark a flagged track as a false positive — hides it from future
    reports without touching the file or blocking anything."""
    data = request.json or {}
    artist, title = data.get("artist", ""), data.get("title", "")
    if not artist or not title:
        return jsonify({"error": "artist and title required"}), 400
    key = track_ignore_key(artist, title)
    whitelist = load_track_mismatch_whitelist()
    whitelist[key] = {"artist": artist, "title": title, "added_at": datetime.utcnow().isoformat()}
    save_track_mismatch_whitelist(whitelist)
    return jsonify({"status": "ok"})

@app.route("/library/mismatched-tracks/remove", methods=["POST"])
def library_mismatched_tracks_remove():
    """Delete a flagged track outright (file + its media_file row, no
    orphaned entry) and blocklist it — for cases with no legitimate
    single-song replacement to flag-wrong to instead (same call as
    /track/flag-wrong otherwise handles)."""
    data = request.json or {}
    path = data.get("path", "")
    artist, title = data.get("artist", ""), data.get("title", "")
    if not path or not artist or not title:
        return jsonify({"error": "path, artist and title required"}), 400

    cfg = load_nd_config()
    ssh_cfg = load_ssh_config()
    ok, err = _delete_track_by_path(path, ssh_cfg, cfg)

    key = track_ignore_key(artist, title)
    ignored = load_ignored_tracks()
    ignored[key] = {
        "artist": artist, "title": title,
        "added_at": datetime.utcnow().isoformat(),
        "reason": "Removed as a mismatched track via /library/mismatched-tracks",
    }
    save_ignored_tracks(ignored)

    return jsonify({"status": "ok" if ok else "partial", "error": err})


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

def _download_and_replace_track(artist, title, album, album_artist, duration_ms,
                                 playlist_name, url, ssh_cfg, nd_cfg,
                                 delete_existing_remote=False):
    """Download `url`, validate it actually looks like the expected track,
    tag it, and sync it to Navidrome. Shared by /failed/retry (retrying a
    recorded failure) and /track/flag-wrong (replacing a track that's
    already in the library — the download looked successful at the time,
    it just turned out to be the wrong audio, e.g. a title collision with a
    different song). Returns a plain dict, ready to jsonify."""
    album = album or "Unknown Album"
    playlist_name = playlist_name or "Manual Downloads"
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

    # Unlike a failed download (nothing to overwrite), a flagged-wrong track
    # already has a file sitting in Navidrome under this exact name — delete
    # it up front so a rejected replacement doesn't leave the known-wrong
    # file in place, and so a stale Navidrome DB row doesn't survive
    # alongside the new one if the filename ever ends up differing.
    if delete_existing_remote and ssh_cfg:
        remote_path = f"{ssh_cfg['music_path']}/{sanitize(album)}/{filename}.flac"
        rm_cmd = _ssh_cmd(ssh_cfg, f"rm -f -- {shlex.quote(remote_path)}")
        result = subprocess.run(rm_cmd, capture_output=True, text=True, timeout=15)
        with job_lock:
            jobs[tmp_job_id]["log"].append(f"🗑 Removed existing file at {remote_path}")
        print(f"[flag-wrong] rm '{remote_path}' -> rc={result.returncode} err={result.stderr}", file=sys.stderr)

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
        return {"success": False, "message": "Download timed out after 30s"}
    if killed == "skipped":
        return {"success": False, "message": "Download was skipped"}
    if rc != 0:
        reason = _extract_yt_dlp_error(stderr) or f"yt-dlp exited with code {rc}"
        return {"success": False, "message": reason}

    flac_path = out_template.replace(".%(ext)s", ".flac")
    if not os.path.exists(flac_path):
        return {"success": False, "message": "File missing after download"}

    wrong_reason = _downloaded_file_looks_wrong(flac_path, title, duration_ms, expected_artist=artist)
    if wrong_reason:
        try:
            os.remove(flac_path)
        except Exception:
            pass
        with job_lock:
            jobs[tmp_job_id]["log"].append(f"✗ Rejected pasted link ({wrong_reason}): {artist} - {title}")
        return {"success": False, "message": f"Rejected — {wrong_reason}. Try a different link."}

    source_url = extract_resolved_url(stdout) or url
    genre = lookup_genre(artist)
    fix_tags(flac_path, title, artist, album, album_artist=album_artist,
             source_url=source_url, genre=genre)
    new_album, flac_path = maybe_correct_album(
        flac_path, title, artist, album, playlist_name, source_url, local_dir, album_artist=album_artist)

    synced_to_navidrome = False
    if ssh_cfg:
        retried_track = {"id": None, "name": title, "artist": artist, "album": new_album,
                          "album_artist": album_artist, "duration_ms": duration_ms,
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
    return {"success": True, "message": message}

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

    result = _download_and_replace_track(
        entry.get("artist", ""), entry.get("name", ""), entry.get("album"),
        entry.get("album_artist"), entry.get("duration_ms", 0),
        entry.get("playlist_name"), url,
        load_ssh_config(), load_nd_config(), delete_existing_remote=False)
    return jsonify(result)

@app.route("/track/flag-wrong", methods=["POST"])
def flag_wrong_track():
    """Replace a track that's already in the library but turned out to be
    the wrong audio under a correct-looking name — most often a title
    collision with a different artist's or soundtrack's same-titled song
    (see the title-collision checks above). Unlike /failed/retry, this track was never a recorded
    failure — the original download looked successful — so the caller
    supplies the track's own expected metadata directly instead of a
    failed_tracks key, and the existing file gets deleted up front rather
    than merely overwritten."""
    body = request.json or {}
    artist = (body.get("artist") or "").strip()
    title = (body.get("name") or "").strip()
    url = (body.get("url") or "").strip()
    if not artist or not title or not url:
        return jsonify({"error": "artist, name and url required"}), 400

    result = _download_and_replace_track(
        artist, title, body.get("album"), body.get("album_artist"),
        body.get("duration_ms", 0), body.get("playlist_name"), url,
        load_ssh_config(), load_nd_config(), delete_existing_remote=True)
    return jsonify(result)

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
