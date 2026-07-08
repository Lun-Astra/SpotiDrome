#!/bin/bash
set -e
cd /opt/muziekapp/spotidrome

cat > backend/app.py << 'PYEOF'
import os, json, threading, time, re, subprocess, shutil
from datetime import datetime
import requests as http
from flask import Flask, jsonify, request
from flask_cors import CORS
import spotipy
from spotipy.oauth2 import SpotifyOAuth

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR          = "/downloads"
SPOTIFY_CLIENT_ID     = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
SPOTIFY_REDIRECT_URI  = os.environ.get("SPOTIFY_REDIRECT_URI", "http://localhost:8080/callback")
NAVIDROME_CONFIG_FILE = "/root/.ssh/navidrome_config.json"
SSH_CONFIG_FILE       = "/root/.ssh/ssh_config.json"
TRACKED_FILE          = "/root/.ssh/tracked_playlists.json"

jobs = {}
job_lock = threading.Lock()

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

def remote_file_exists(remote_path, cfg):
    """Check if a file exists on the remote server."""
    try:
        cmd = ["ssh", "-i", "/root/.ssh/id_rsa",
               "-p", str(cfg["port"]),
               "-o", "StrictHostKeyChecking=no",
               "-o", "BatchMode=yes",
               "-o", "ConnectTimeout=5",
               f"{cfg['user']}@{cfg['host']}",
               f"test -f '{remote_path}' && echo found || echo notfound"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return result.stdout.strip() == "found"
    except Exception:
        return False

def rsync_to_remote(local_dir, cfg, job_id=None):
    def log(msg):
        if job_id:
            with job_lock:
                jobs[job_id]["log"].append(msg)
    dest = f"{cfg['user']}@{cfg['host']}:{cfg['music_path']}/"
    folder_name = os.path.basename(local_dir)
    cmd = ["rsync", "-avz",
           "-e", f"ssh -i /root/.ssh/id_rsa -p {cfg['port']} -o StrictHostKeyChecking=no -o BatchMode=yes",
           local_dir + "/",
           f"{dest}{folder_name}/"]
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

def nd_trigger_scan(cfg=None):
    if cfg is None:
        cfg = load_nd_config()
    try:
        resp = http.put(f"{cfg['url']}/api/scanner/trigger",
                        auth=(cfg["user"], cfg["password"]), timeout=15)
        resp.raise_for_status()
        return True, "Library scan triggered"
    except Exception as e1:
        try:
            nd_subsonic("startScan", cfg=cfg)
            return True, "Library scan triggered (subsonic)"
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
    try:
        data = nd_subsonic("getPlaylists", cfg=cfg)
        for pl in data.get("playlists", {}).get("playlist", []):
            if pl["name"].lower() == name.lower():
                return pl["id"], False
    except Exception:
        pass
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
    nd_subsonic("updatePlaylist", cfg=cfg, playlistId=pl_id, **{"songIdToAdd": song_ids})
    log(f"✅ {'Created' if created else 'Updated'} playlist '{playlist_name}' — {len(song_ids)} tracks")
    if not_found:
        log(f"⚠ {len(not_found)} track(s) not matched")
    return len(song_ids), len(not_found)

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
                           "artist": ", ".join(a["name"] for a in t["artists"]),
                           "album": t["album"]["name"], "duration_ms": t["duration_ms"],
                           "image": t["album"]["images"][0]["url"] if t["album"].get("images") else None})
        if not batch["next"]: break
        offset += 100
    return tracks

# ─── Download worker ──────────────────────────────────────────────────────────

def sanitize(name):
    return re.sub(r'[\\/*?:"<>|]', "_", name)

def download_worker(job_id, tracks, playlist_name, playlist_id=None, sync_navidrome=True):
    with job_lock:
        jobs[job_id]["status"] = "running"

    ssh_cfg = load_ssh_config()
    local_playlist_dir = os.path.join(DOWNLOAD_DIR, sanitize(playlist_name))
    os.makedirs(local_playlist_dir, exist_ok=True)

    # Tracks that need to go into the Navidrome playlist (already on remote OR newly downloaded)
    all_synced_tracks = []
    # Tracks that were newly downloaded and need rsyncing
    newly_downloaded = []

    for i, track in enumerate(tracks):
        with job_lock:
            jobs[job_id]["current"] = i + 1
            jobs[job_id]["current_track"] = f"{track['artist']} - {track['name']}"

        filename = sanitize(f"{track['artist']} - {track['name']}")
        out_template = os.path.join(local_playlist_dir, f"{filename}.%(ext)s")
        remote_path = f"{ssh_cfg['music_path']}/{sanitize(playlist_name)}/{filename}.mp3" if ssh_cfg else ""

        # Check if already on remote Navidrome server
        if ssh_cfg and remote_file_exists(remote_path, ssh_cfg):
            with job_lock:
                jobs[job_id]["log"].append(f"⏭ Already on Navidrome: {track['artist']} - {track['name']}")
            all_synced_tracks.append(track)
            continue

        # Check if already downloaded locally (waiting for rsync)
        local_exists = [f for f in os.listdir(local_playlist_dir) if f.startswith(filename)]
        if local_exists:
            with job_lock:
                jobs[job_id]["log"].append(f"⏭ Already downloaded locally: {track['artist']} - {track['name']}")
            newly_downloaded.append(track)
            all_synced_tracks.append(track)
            continue

        # Download fresh
        cmd = ["yt-dlp", "--default-search", "https://music.youtube.com/search?q=",
               "-x", "--audio-format", "mp3", "--audio-quality", "0",
               "--add-metadata", "--embed-thumbnail", "--output", out_template,
               "--no-playlist", "--match-filter", "duration > 60",
               f"ytsearch1:{track['artist']} - {track['name']} audio"]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0:
                with job_lock:
                    jobs[job_id]["log"].append(f"✓ Downloaded: {track['artist']} - {track['name']}")
                    jobs[job_id]["downloaded"] += 1
                newly_downloaded.append(track)
                all_synced_tracks.append(track)
            else:
                with job_lock:
                    jobs[job_id]["log"].append(f"✗ Failed: {track['artist']} - {track['name']}")
                    jobs[job_id]["failed"] += 1
        except subprocess.TimeoutExpired:
            with job_lock:
                jobs[job_id]["log"].append(f"✗ Timeout: {track['artist']} - {track['name']}")
                jobs[job_id]["failed"] += 1
        except Exception as e:
            with job_lock:
                jobs[job_id]["log"].append(f"✗ Error: {track['artist']} - {track['name']}: {e}")
                jobs[job_id]["failed"] += 1

    # Track playlist for auto-sync
    if playlist_id and all_synced_tracks:
        track_playlist(playlist_id, playlist_name, tracks)

    # Rsync only if there are newly downloaded files
    rsync_ok = False
    if newly_downloaded and ssh_cfg:
        with job_lock:
            jobs[job_id]["status"] = "uploading"
            jobs[job_id]["current_track"] = f"Uploading {len(newly_downloaded)} tracks to {ssh_cfg['host']}…"
        rsync_ok = rsync_to_remote(local_playlist_dir, ssh_cfg, job_id)

        # Delete local files only after successful rsync
        if rsync_ok:
            with job_lock:
                jobs[job_id]["current_track"] = "Cleaning up local files…"
            try:
                shutil.rmtree(local_playlist_dir)
                with job_lock:
                    jobs[job_id]["log"].append("🗑 Local temp files deleted")
            except Exception as e:
                with job_lock:
                    jobs[job_id]["log"].append(f"⚠ Cleanup failed: {e}")
        else:
            with job_lock:
                jobs[job_id]["log"].append("⚠ Skipping cleanup — rsync failed, files kept locally for retry")
    elif not newly_downloaded:
        with job_lock:
            jobs[job_id]["log"].append("ℹ All tracks already on Navidrome — skipping rsync")
        rsync_ok = True  # Nothing to rsync, proceed to playlist sync

    # Navidrome scan + playlist sync
    nd_cfg = load_nd_config()
    if sync_navidrome and nd_cfg and all_synced_tracks:
        if newly_downloaded:
            # Only scan if we uploaded new files
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

# ─── Auto-sync scheduler ──────────────────────────────────────────────────────

def auto_sync_worker():
    print("[auto-sync] Starting nightly sync…")
    tracked = load_tracked()
    if not tracked:
        print("[auto-sync] No tracked playlists, skipping.")
        return
    sp, _ = get_sp()
    if not sp:
        print("[auto-sync] Spotify not authenticated, skipping.")
        return
    for playlist_id, info in tracked.items():
        playlist_name = info["name"]
        print(f"[auto-sync] Syncing: {playlist_name}")
        try:
            tracks = fetch_playlist_tracks(sp, playlist_id)
        except Exception as e:
            print(f"[auto-sync] Failed to fetch tracks for {playlist_name}: {e}")
            continue
        job_id = f"auto_{int(time.time()*1000)}"
        with job_lock:
            jobs[job_id] = {"id": job_id, "playlist": f"[Auto] {playlist_name}",
                            "status": "pending", "total": len(tracks), "current": 0,
                            "downloaded": 0, "failed": 0, "nd_synced": None,
                            "nd_missing": None, "current_track": None, "log": []}
        download_worker(job_id, tracks, playlist_name, playlist_id=playlist_id, sync_navidrome=True)
        time.sleep(5)
    print("[auto-sync] Nightly sync complete.")

def scheduler_loop():
    while True:
        now = datetime.utcnow()
        from datetime import timedelta
        target = now.replace(hour=3, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        wait_seconds = (target - now).total_seconds()
        print(f"[scheduler] Next auto-sync in {wait_seconds/3600:.1f} hours")
        time.sleep(wait_seconds)
        threading.Thread(target=auto_sync_worker, daemon=True).start()

threading.Thread(target=scheduler_loop, daemon=True).start()

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
        return jsonify(list(jobs.values()))

@app.route("/jobs/<job_id>")
def get_job(job_id):
    with job_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    return jsonify(job)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
PYEOF

docker compose up -d --build backend
echo "✅ Backend rebuilt with fixed download/skip/rsync logic"
