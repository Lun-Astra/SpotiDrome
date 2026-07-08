#!/bin/bash
set -e
cd /opt/muziekapp/spotidrome

# ── 1. Backend: yt-dlp update + cookies support ───────────────────────────────
python3 << 'PYEOF'
content = open('backend/app.py').read()

new_routes = '''
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
        return jsonify({"error": "Doesn\\'t look like a valid Netscape cookie file"}), 400
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

'''

if '@app.route("/ytdlp/version")' not in content:
    content = content.replace(
        'if __name__ == "__main__":',
        new_routes + 'if __name__ == "__main__":'
    )

# Add cookies to yt-dlp commands when available
old_sp_cmd = '''        cmd = ["yt-dlp", "--default-search", "https://music.youtube.com/search?q=",
               "-x", "--audio-format", "flac", "--audio-quality", "0",
               "--add-metadata", "--embed-thumbnail", "--output", out_template,
               "--no-playlist", "--match-filter", "duration > 60",
               "--retries", "1", "--fragment-retries", "1", "--extractor-retries", "1",
               "--concurrent-fragments", "1", "--socket-timeout", "10",
               "--sleep-interval", "2", "--max-sleep-interval", "4",
               "--no-progress",
               f"ytsearch1:{track['artist']} - {track['name']} audio"]'''

new_sp_cmd = '''        cookies_args = ["--cookies", COOKIES_FILE] if os.path.exists(COOKIES_FILE) else []
        cmd = ["yt-dlp", "--default-search", "https://music.youtube.com/search?q=",
               "-x", "--audio-format", "flac", "--audio-quality", "0",
               "--add-metadata", "--embed-thumbnail", "--output", out_template,
               "--no-playlist", "--match-filter", "duration > 60",
               "--retries", "1", "--fragment-retries", "1", "--extractor-retries", "1",
               "--concurrent-fragments", "1", "--socket-timeout", "10",
               "--sleep-interval", "2", "--max-sleep-interval", "4",
               "--no-progress",
               ] + cookies_args + [f"ytsearch1:{track['artist']} - {track['name']} audio"]'''

old_yt_cmd = '''        cmd = ["yt-dlp",
               "-x", "--audio-format", "flac", "--audio-quality", "0",
               "--add-metadata", "--embed-thumbnail",
               "--output", out_template,
               "--no-playlist" if not is_playlist else "--yes-playlist",
               "--retries", "1", "--fragment-retries", "1", "--extractor-retries", "1",
               "--concurrent-fragments", "1", "--socket-timeout", "10",
               "--sleep-interval", "2", "--max-sleep-interval", "4",
               "--no-progress",
               track_url]'''

new_yt_cmd = '''        cookies_args = ["--cookies", COOKIES_FILE] if os.path.exists(COOKIES_FILE) else []
        cmd = ["yt-dlp",
               "-x", "--audio-format", "flac", "--audio-quality", "0",
               "--add-metadata", "--embed-thumbnail",
               "--output", out_template,
               "--no-playlist" if not is_playlist else "--yes-playlist",
               "--retries", "1", "--fragment-retries", "1", "--extractor-retries", "1",
               "--concurrent-fragments", "1", "--socket-timeout", "10",
               "--sleep-interval", "2", "--max-sleep-interval", "4",
               "--no-progress",
               ] + cookies_args + [track_url]'''

sp_found = old_sp_cmd in content
yt_found = old_yt_cmd in content
print(f"Spotify cmd: {'found' if sp_found else 'NOT FOUND'}")
print(f"YT cmd: {'found' if yt_found else 'NOT FOUND'}")

content = content.replace(old_sp_cmd, new_sp_cmd)
content = content.replace(old_yt_cmd, new_yt_cmd)

open('backend/app.py', 'w').write(content)
print("Backend patched")
PYEOF

# ── 2. Frontend: add settings panel ──────────────────────────────────────────
python3 << 'PYEOF'
content = open('frontend/index.html').read()

settings_css = '''
  .settings-section { margin-bottom: 20px; }
  .settings-section-title { font-size: 11px; color: var(--text-dim); text-transform: uppercase; letter-spacing: .5px; margin-bottom: 12px; }
  .settings-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
  .settings-label { font-size: 13px; }
  .settings-sub { font-size: 11px; color: var(--text-dim); margin-top: 2px; }
  .version-badge { font-size: 11px; padding: 3px 10px; border-radius: 20px; background: var(--surface2); border: 1px solid var(--border); color: var(--text-mid); }
  .cookie-status { font-size: 11px; padding: 3px 10px; border-radius: 20px; }
  .cookie-status.active { background: rgba(29,185,84,.1); color: var(--accent); }
  .cookie-status.inactive { background: var(--surface2); color: var(--text-dim); }
  .cookie-textarea {
    width: 100%; height: 100px; background: var(--surface2); border: 1px solid var(--border);
    border-radius: 6px; color: var(--text); font-family: var(--font-mono); font-size: 11px;
    padding: 8px; resize: vertical; outline: none;
  }
  .cookie-textarea:focus { border-color: var(--accent); }
'''

content = content.replace('</style>', settings_css + '</style>')

# Add settings pill to header
content = content.replace(
    '<div class="nd-pill" onclick="openNdModal()" id="nd-pill">',
    '<div class="pill" onclick="openSettingsModal()" title="Settings">⚙</div>\n      <div class="nd-pill" onclick="openNdModal()" id="nd-pill">'
)

# Add settings modal
settings_modal = '''
<div class="modal-overlay" id="settings-modal">
  <div class="modal" style="max-width:500px">
    <div class="modal-header">
      <div class="modal-title">⚙ Settings</div>
      <button class="modal-close" onclick="closeSettingsModal()">✕</button>
    </div>
    <div class="modal-body">

      <!-- yt-dlp section -->
      <div class="settings-section">
        <div class="settings-section-title">yt-dlp</div>
        <div class="settings-row">
          <div>
            <div class="settings-label">Current version</div>
            <div class="settings-sub">Used for all downloads</div>
          </div>
          <span class="version-badge" id="ytdlp-version">Loading…</span>
        </div>
        <div class="settings-row">
          <div>
            <div class="settings-label">Update yt-dlp</div>
            <div class="settings-sub">Fixes broken YouTube downloads</div>
          </div>
          <button class="btn btn-outline" id="btn-update-ytdlp" onclick="updateYtdlp()">⬆ Update</button>
        </div>
        <div id="ytdlp-update-output" style="display:none;margin-top:8px;background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:10px;font-size:11px;color:var(--text-mid);white-space:pre-wrap;max-height:100px;overflow-y:auto"></div>
      </div>

      <!-- Cookies section -->
      <div class="settings-section">
        <div class="settings-section-title">YouTube Cookies</div>
        <div class="settings-row">
          <div>
            <div class="settings-label">Cookie status</div>
            <div class="settings-sub">Reduces rate limiting on large playlists</div>
          </div>
          <span class="cookie-status inactive" id="cookie-status-badge">Not set</span>
        </div>
        <div class="field">
          <label>Paste Netscape cookie file contents</label>
          <textarea class="cookie-textarea" id="cookie-content" placeholder="# Netscape HTTP Cookie File&#10;.youtube.com	TRUE	/	FALSE	..."></textarea>
          <div class="field-hint">Export from browser using the "Get cookies.txt LOCALLY" extension. Only YouTube cookies needed.</div>
        </div>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:8px">
          <button class="btn btn-outline" id="btn-delete-cookies" onclick="deleteCookies()">🗑 Remove</button>
          <button class="btn btn-primary" onclick="saveCookies()">💾 Save Cookies</button>
        </div>
      </div>

    </div>
  </div>
</div>
'''

content = content.replace('<div class="modal-overlay" id="nd-modal">', settings_modal + '<div class="modal-overlay" id="nd-modal">')

# Add JS
settings_js = '''
// ── Settings ──────────────────────────────────────────────────────────────────
function openSettingsModal() {
  loadYtdlpVersion();
  loadCookieStatus();
  document.getElementById('settings-modal').classList.add('show');
}
function closeSettingsModal() { document.getElementById('settings-modal').classList.remove('show'); }

async function loadYtdlpVersion() {
  try {
    const res = await fetch(`${API}/ytdlp/version`);
    const data = await res.json();
    document.getElementById('ytdlp-version').textContent = data.version || 'Unknown';
  } catch(e) {
    document.getElementById('ytdlp-version').textContent = 'Error';
  }
}

async function updateYtdlp() {
  const btn = document.getElementById('btn-update-ytdlp');
  const out = document.getElementById('ytdlp-update-output');
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Updating…';
  out.style.display = 'none';
  try {
    const res = await fetch(`${API}/ytdlp/update`, { method: 'POST' });
    const data = await res.json();
    out.style.display = 'block';
    out.textContent = data.output || data.error || 'Done';
    await loadYtdlpVersion();
  } catch(e) {
    out.style.display = 'block';
    out.textContent = 'Error: ' + e.message;
  } finally {
    btn.disabled = false; btn.innerHTML = '⬆ Update';
  }
}

async function loadCookieStatus() {
  try {
    const res = await fetch(`${API}/cookies/status`);
    const data = await res.json();
    const badge = document.getElementById('cookie-status-badge');
    if (data.configured) {
      badge.textContent = `Active · ${data.modified}`;
      badge.className = 'cookie-status active';
    } else {
      badge.textContent = 'Not set';
      badge.className = 'cookie-status inactive';
    }
  } catch(e) {}
}

async function saveCookies() {
  const content_val = document.getElementById('cookie-content').value.trim();
  if (!content_val) { alert('Paste your cookie file contents first.'); return; }
  try {
    const res = await fetch(`${API}/cookies/upload`, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({content: content_val}),
    });
    const data = await res.json();
    if (data.error) { alert('Error: ' + data.error); return; }
    document.getElementById('cookie-content').value = '';
    await loadCookieStatus();
    alert('✓ Cookies saved! All future downloads will use them.');
  } catch(e) { alert('Error: ' + e.message); }
}

async function deleteCookies() {
  if (!confirm('Remove YouTube cookies?')) return;
  await fetch(`${API}/cookies/delete`, { method: 'DELETE' });
  await loadCookieStatus();
}

'''

content = content.replace('async function init(){', settings_js + 'async function init(){')
open('frontend/index.html', 'w').write(content)
print("Frontend patched")
PYEOF

docker compose up -d --build
echo "✅ yt-dlp update + cookies support added"
