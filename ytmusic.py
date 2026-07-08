#!/bin/bash
set -e
cd /opt/muziekapp/spotidrome

# ── 1. Backend: add YT Music download endpoint ───────────────────────────────
python3 << 'PYEOF'
content = open('backend/app.py').read()

yt_functions = '''
# ─── YouTube Music helpers ────────────────────────────────────────────────────

def ytmusic_get_info(url):
    """Extract playlist/album/track info from a YouTube Music URL."""
    cmd = ["yt-dlp", "--dump-json", "--flat-playlist",
           "--no-playlist" if "watch?v=" in url and "list=" not in url else "--yes-playlist",
           url]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise ValueError(f"yt-dlp error: {result.stderr[-300:]}")
    entries = []
    for line in result.stdout.strip().split("\\n"):
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
    local_dir = os.path.join(DOWNLOAD_DIR, sanitize(playlist_name))
    os.makedirs(local_dir, exist_ok=True)

    # Get track list first
    with job_lock:
        jobs[job_id]["current_track"] = "Fetching track list from YouTube Music…"
    try:
        entries = ytmusic_get_info(url)
    except Exception as e:
        with job_lock:
            jobs[job_id]["log"].append(f"✗ Failed to fetch info: {e}")
            jobs[job_id]["status"] = "done"
            jobs[job_id]["current_track"] = None
        return

    with job_lock:
        jobs[job_id]["total"] = len(entries)
        jobs[job_id]["log"].append(f"ℹ Found {len(entries)} track(s)")

    downloaded_tracks = []
    yt_track_list = []

    for i, entry in enumerate(entries):
        track_id = entry.get("id") or entry.get("url", "")
        title = entry.get("title", "Unknown Title")
        artist = entry.get("uploader") or entry.get("channel") or entry.get("artist") or "Unknown Artist"
        # Clean up artist (remove " - Topic" suffix from YouTube Music)
        artist = re.sub(r" - Topic$", "", artist)
        album = entry.get("album") or playlist_name
        track_url = f"https://www.youtube.com/watch?v={track_id}" if not track_id.startswith("http") else track_id

        with job_lock:
            jobs[job_id]["current"] = i + 1
            jobs[job_id]["current_track"] = f"{artist} - {title}"

        filename = sanitize(f"{artist} - {title}")
        out_template = os.path.join(local_dir, f"{filename}.%(ext)s")
        remote_path = f"{ssh_cfg['music_path']}/{sanitize(playlist_name)}/{filename}.flac" if ssh_cfg else ""

        # Check remote
        if ssh_cfg and remote_file_exists(remote_path, ssh_cfg):
            with job_lock:
                jobs[job_id]["log"].append(f"⏭ Already on Navidrome: {artist} - {title}")
            t = {"id": track_id, "name": title, "artist": artist, "album": album, "duration_ms": 0, "image": None}
            downloaded_tracks.append(t)
            yt_track_list.append(t)
            continue

        # Check local
        if [f for f in os.listdir(local_dir) if f.startswith(filename)]:
            with job_lock:
                jobs[job_id]["log"].append(f"⏭ Already downloaded locally: {artist} - {title}")
            t = {"id": track_id, "name": title, "artist": artist, "album": album, "duration_ms": 0, "image": None}
            downloaded_tracks.append(t)
            yt_track_list.append(t)
            continue

        cmd = ["yt-dlp",
               "-x", "--audio-format", "flac", "--audio-quality", "0",
               "--add-metadata", "--embed-thumbnail",
               "--output", out_template,
               "--no-playlist" if not is_playlist else "--yes-playlist",
               "--retries", "3", "--fragment-retries", "3",
               "--sleep-interval", "2", "--max-sleep-interval", "5",
               "--concurrent-fragments", "1",
               track_url]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode == 0:
                flac_path = out_template.replace(".%(ext)s", ".flac")
                fix_tags(flac_path, title, artist, album)
                t = {"id": track_id, "name": title, "artist": artist, "album": album, "duration_ms": 0, "image": None}
                downloaded_tracks.append(t)
                yt_track_list.append(t)
                with job_lock:
                    jobs[job_id]["log"].append(f"✓ Downloaded: {artist} - {title}")
                    jobs[job_id]["downloaded"] += 1
            else:
                with job_lock:
                    jobs[job_id]["log"].append(f"✗ Failed: {artist} - {title}")
                    jobs[job_id]["failed"] += 1
        except subprocess.TimeoutExpired:
            with job_lock:
                jobs[job_id]["log"].append(f"✗ Timeout: {artist} - {title}")
                jobs[job_id]["failed"] += 1
        except Exception as e:
            with job_lock:
                jobs[job_id]["log"].append(f"✗ Error: {artist} - {title}: {e}")
                jobs[job_id]["failed"] += 1

    # Track for auto-sync (use URL as ID)
    if is_playlist and yt_track_list:
        url_id = f"yt_{re.sub(r'[^a-zA-Z0-9]', '_', url)[:60]}"
        track_playlist(url_id, playlist_name, yt_track_list)
        with job_lock:
            jobs[job_id]["log"].append(f"📌 Tracked for auto-sync")

    # Rsync
    rsync_ok = False
    if downloaded_tracks and ssh_cfg:
        new_files = [t for t in downloaded_tracks]
        actually_new = [f for f in os.listdir(local_dir) if f.endswith('.flac')]
        if actually_new:
            with job_lock:
                jobs[job_id]["status"] = "uploading"
                jobs[job_id]["current_track"] = f"Uploading to {ssh_cfg['host']}…"
            rsync_ok = rsync_to_remote(local_dir, ssh_cfg, job_id)
            if rsync_ok:
                try:
                    import shutil as _shutil
                    _shutil.rmtree(local_dir)
                    with job_lock:
                        jobs[job_id]["log"].append("🗑 Local temp files deleted")
                except Exception as e:
                    with job_lock:
                        jobs[job_id]["log"].append(f"⚠ Cleanup failed: {e}")
        else:
            rsync_ok = True

    # Navidrome scan + playlist sync
    nd_cfg = load_nd_config()
    if nd_cfg and downloaded_tracks and rsync_ok:
        if any(t for t in downloaded_tracks):
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

'''

# Add YT route
yt_route = '''
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

'''

if 'def ytmusic_download_worker' not in content:
    content = content.replace('def scheduler_loop():', yt_functions + 'def scheduler_loop():')

if '@app.route("/ytmusic/info"' not in content:
    content = content.replace("if __name__ == \"__main__\":", yt_route + "if __name__ == \"__main__\":")

open('backend/app.py', 'w').write(content)
print("Backend patched with YouTube Music support")
PYEOF

# ── 2. Frontend: add YT Music panel ──────────────────────────────────────────
python3 << 'PYEOF'
content = open('frontend/index.html').read()

# Add CSS
yt_css = '''
  .yt-panel { margin-bottom: 20px; }
  .yt-input-row { display: flex; gap: 10px; padding: 16px 20px; border-bottom: 1px solid var(--border); }
  .yt-input {
    flex: 1; background: var(--surface2); border: 1px solid var(--border);
    border-radius: 6px; color: var(--text); font-family: var(--font-mono);
    font-size: 13px; padding: 10px 14px; outline: none; transition: border-color .15s;
  }
  .yt-input:focus { border-color: #ff0000; }
  .btn-yt { background: #ff0000; color: #fff; }
  .btn-yt:hover { box-shadow: 0 4px 16px rgba(255,0,0,.3); transform: translateY(-1px); }
  .btn-yt:disabled { opacity: .5; cursor: not-allowed; transform: none; box-shadow: none; }
  .yt-preview {
    display: none; padding: 14px 20px; gap: 14px; align-items: center;
    border-bottom: 1px solid var(--border); background: var(--surface2);
  }
  .yt-preview.show { display: flex; }
  .yt-preview-thumb { width: 56px; height: 56px; border-radius: 6px; object-fit: cover; background: var(--border); flex-shrink: 0; }
  .yt-preview-info { flex: 1; overflow: hidden; }
  .yt-preview-title { font-size: 14px; font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .yt-preview-meta { font-size: 12px; color: var(--text-dim); margin-top: 4px; }
  .yt-type-badge {
    font-size: 10px; padding: 2px 8px; border-radius: 20px; font-weight: 700;
    background: rgba(255,0,0,.15); color: #ff4444;
  }
  .yt-type-badge.playlist { background: rgba(108,99,255,.15); color: var(--nd); }
'''

content = content.replace(
    '#callback-overlay.show { display: flex; }',
    '#callback-overlay.show { display: flex; }' + yt_css
)

# Add YT panel to main content, before the main grid
yt_panel_html = '''
        <!-- YouTube Music Panel -->
        <div class="panel yt-panel" style="margin-bottom:24px">
          <div class="panel-header">
            <span class="panel-title" style="display:flex;align-items:center;gap:8px">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="#ff0000"><path d="M23.498 6.186a3.016 3.016 0 0 0-2.122-2.136C19.505 3.545 12 3.545 12 3.545s-7.505 0-9.377.505A3.017 3.017 0 0 0 .502 6.186C0 8.07 0 12 0 12s0 3.93.502 5.814a3.016 3.016 0 0 0 2.122 2.136c1.871.505 9.376.505 9.376.505s7.505 0 9.377-.505a3.015 3.015 0 0 0 2.122-2.136C24 15.93 24 12 24 12s0-3.93-.502-5.814zM9.545 15.568V8.432L15.818 12l-6.273 3.568z"/></svg>
              YOUTUBE MUSIC
            </span>
          </div>
          <div class="yt-input-row">
            <input class="yt-input" id="yt-url" type="url" placeholder="Paste a YouTube Music URL (track, album or playlist)…">
            <button class="btn btn-yt" id="btn-yt-fetch" onclick="ytFetch()">Fetch</button>
          </div>
          <div class="yt-preview" id="yt-preview">
            <img class="yt-preview-thumb" id="yt-thumb" src="">
            <div class="yt-preview-info">
              <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
                <div class="yt-preview-title" id="yt-title"></div>
                <span class="yt-type-badge" id="yt-type-badge"></span>
              </div>
              <div class="yt-preview-meta" id="yt-meta"></div>
            </div>
            <button class="btn btn-yt" id="btn-yt-download" onclick="ytDownload()" disabled>⬇ Download</button>
          </div>
        </div>
'''

content = content.replace(
    '<div class="main">',
    yt_panel_html + '<div class="main">'
)

# Add JS
yt_js = '''
// ── YouTube Music ─────────────────────────────────────────────────────────────
let ytInfo = null;

async function ytFetch() {
  const url = document.getElementById('yt-url').value.trim();
  if (!url) return;
  const btn = document.getElementById('btn-yt-fetch');
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>';
  document.getElementById('yt-preview').classList.remove('show');
  document.getElementById('btn-yt-download').disabled = true;

  try {
    const res = await fetch(`${API}/ytmusic/info`, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({url}),
    });
    const data = await res.json();
    if (data.error) { alert('Error: ' + data.error); return; }

    ytInfo = {...data, url};
    document.getElementById('yt-title').textContent = data.title;
    document.getElementById('yt-meta').textContent = `${data.uploader}${data.is_playlist ? ` · ${data.track_count} tracks` : ''}`;
    document.getElementById('yt-thumb').src = data.thumbnail || '';
    const badge = document.getElementById('yt-type-badge');
    badge.textContent = data.is_playlist ? 'PLAYLIST' : 'TRACK';
    badge.className = 'yt-type-badge' + (data.is_playlist ? ' playlist' : '');
    document.getElementById('yt-preview').classList.add('show');
    document.getElementById('btn-yt-download').disabled = false;
  } catch(e) {
    alert('Failed to fetch info: ' + e.message);
  } finally {
    btn.disabled = false; btn.innerHTML = 'Fetch';
  }
}

async function ytDownload() {
  if (!ytInfo) return;
  const btn = document.getElementById('btn-yt-download');
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>';
  try {
    await fetch(`${API}/ytmusic/download`, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        url: ytInfo.url,
        playlist_name: ytInfo.title,
        is_playlist: ytInfo.is_playlist,
      }),
    });
    document.getElementById('yt-url').value = '';
    document.getElementById('yt-preview').classList.remove('show');
    ytInfo = null;
    renderJobs();
  } catch(e) {
    alert('Failed to start download: ' + e.message);
  } finally {
    btn.disabled = false; btn.innerHTML = '⬇ Download';
  }
}

document.getElementById('yt-url').addEventListener('keydown', e => {
  if (e.key === 'Enter') ytFetch();
});
'''

content = content.replace(
    'async function init(){',
    yt_js + 'async function init(){'
)

open('frontend/index.html', 'w').write(content)
print("Frontend patched with YouTube Music panel")
PYEOF

# ── 3. Rebuild ────────────────────────────────────────────────────────────────
docker compose up -d --build
echo "✅ YouTube Music support added and rebuilt"
