#!/bin/bash
set -e
cd /opt/muziekapp/spotidrome

# ── 1. Backend: add cleanup endpoints ────────────────────────────────────────
python3 << 'PYEOF'
content = open('backend/app.py').read()

cleanup_routes = '''
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
                if path:
                    paths_to_delete.append(path)
            except Exception:
                pass

    # Delete from Navidrome by removing from all playlists and marking as deleted
    # Use subsonic star/unstar doesn\'t delete - we need to delete the file
    # Delete files via SSH
    if delete_files and ssh_cfg and paths_to_delete:
        for path in paths_to_delete:
            try:
                # Path from Navidrome is relative to music folder
                full_path = path if path.startswith("/") else f"{ssh_cfg['music_path']}/{path}"
                cmd = ["ssh", "-i", "/root/.ssh/id_rsa",
                       "-p", str(ssh_cfg["port"]),
                       "-o", "StrictHostKeyChecking=no",
                       "-o", "BatchMode=yes",
                       f"{ssh_cfg['user']}@{ssh_cfg['host']}",
                       f"rm -f \\"{full_path}\\""]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
                if result.returncode == 0:
                    deleted.append(path)
                else:
                    failed.append(path)
            except Exception as e:
                failed.append(f"{path}: {e}")

    # Trigger rescan so Navidrome removes deleted files
    if deleted:
        nd_trigger_scan(cfg)

    return jsonify({
        "deleted_files": len(deleted),
        "failed": len(failed),
        "failed_list": failed[:10],
    })

'''

if '@app.route("/cleanup/scan"' not in content:
    content = content.replace(
        'if __name__ == "__main__":',
        cleanup_routes + 'if __name__ == "__main__":'
    )
    open('backend/app.py', 'w').write(content)
    print("Backend patched")
else:
    print("Already patched")
PYEOF

# ── 2. Frontend: add cleanup widget ──────────────────────────────────────────
python3 << 'PYEOF'
content = open('frontend/index.html').read()

cleanup_css = '''
  /* Cleanup widget */
  .cleanup-modal .modal { max-width: 800px; max-height: 90vh; display: flex; flex-direction: column; }
  .cleanup-tabs { display: flex; border-bottom: 1px solid var(--border); }
  .cleanup-tab {
    padding: 12px 20px; cursor: pointer; font-size: 12px; font-weight: 700;
    font-family: var(--font-mono); color: var(--text-dim); border-bottom: 2px solid transparent;
    transition: all .15s; background: none; border-top: none; border-left: none; border-right: none;
  }
  .cleanup-tab.active { color: var(--accent2); border-bottom-color: var(--accent2); }
  .cleanup-list { overflow-y: auto; max-height: 50vh; flex: 1; }
  .cleanup-item {
    display: flex; align-items: center; gap: 12px; padding: 10px 20px;
    border-bottom: 1px solid var(--border); transition: background .12s;
  }
  .cleanup-item:hover { background: var(--surface2); }
  .cleanup-item input[type=checkbox] { accent-color: var(--accent2); width: 16px; height: 16px; flex-shrink: 0; cursor: pointer; }
  .cleanup-info { flex: 1; overflow: hidden; }
  .cleanup-title { font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .cleanup-meta { font-size: 11px; color: var(--text-dim); margin-top: 2px; }
  .cleanup-reason {
    font-size: 10px; padding: 2px 8px; border-radius: 20px; font-weight: 700; flex-shrink: 0;
  }
  .cleanup-reason.duplicate { background: rgba(255,77,109,.15); color: var(--accent2); }
  .cleanup-reason.long { background: rgba(245,158,11,.15); color: var(--ssh); }
  .cleanup-footer { padding: 14px 20px; border-top: 1px solid var(--border); display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  .cleanup-stats { font-size: 12px; color: var(--text-dim); flex: 1; }
  .btn-danger { background: var(--accent2); color: #fff; }
  .btn-danger:hover { box-shadow: 0 4px 16px rgba(255,77,109,.3); transform: translateY(-1px); }
  .btn-danger:disabled { opacity: .5; cursor: not-allowed; transform: none; box-shadow: none; }
'''

content = content.replace('</style>', cleanup_css + '</style>')

# Add cleanup button to header
content = content.replace(
    '<div class="nd-pill" onclick="openNdModal()" id="nd-pill">',
    '<div class="pill" onclick="openCleanupModal()" style="border-color:rgba(255,77,109,.3);color:var(--accent2)" title="Find duplicates & long tracks">🧹 Cleanup</div>\n      <div class="nd-pill" onclick="openNdModal()" id="nd-pill">'
)

# Add cleanup modal
cleanup_modal = '''
<div class="modal-overlay cleanup-modal" id="cleanup-modal">
  <div class="modal" style="max-width:800px">
    <div class="modal-header">
      <div class="modal-title" style="color:var(--accent2)">🧹 Library Cleanup</div>
      <button class="modal-close" onclick="closeCleanupModal()">✕</button>
    </div>
    <div class="cleanup-tabs">
      <button class="cleanup-tab active" id="tab-dupes" onclick="switchTab('dupes')">
        Duplicates <span id="dupe-count-badge">(0)</span>
      </button>
      <button class="cleanup-tab" id="tab-long" onclick="switchTab('long')">
        Long tracks &gt;10min <span id="long-count-badge">(0)</span>
      </button>
    </div>
    <div id="cleanup-content" class="cleanup-list">
      <div class="empty-state"><div class="icon">🔍</div>Click Scan to find issues</div>
    </div>
    <div class="cleanup-footer">
      <div class="cleanup-stats" id="cleanup-stats">Not scanned yet</div>
      <label style="display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer">
        <input type="checkbox" id="cleanup-delete-files" checked style="accent-color:var(--accent2)">
        Delete files from disk
      </label>
      <button class="btn btn-outline" id="btn-scan" onclick="runCleanupScan()">🔍 Scan</button>
      <button class="btn btn-outline" onclick="selectAllCleanup()">Select All</button>
      <button class="btn btn-danger" id="btn-delete-selected" disabled onclick="deleteSelected()">🗑 Delete Selected</button>
    </div>
  </div>
</div>
'''

content = content.replace('<div class="modal-overlay" id="nd-modal">', cleanup_modal + '<div class="modal-overlay" id="nd-modal">')

# Add cleanup JS
cleanup_js = '''
// ── Cleanup ───────────────────────────────────────────────────────────────────
let cleanupData = {duplicates: [], long_tracks: []};
let cleanupTab = 'dupes';

function openCleanupModal() {
  document.getElementById('cleanup-modal').classList.add('show');
}
function closeCleanupModal() {
  document.getElementById('cleanup-modal').classList.remove('show');
}

function switchTab(tab) {
  cleanupTab = tab;
  document.getElementById('tab-dupes').classList.toggle('active', tab === 'dupes');
  document.getElementById('tab-long').classList.toggle('active', tab === 'long');
  renderCleanupList();
}

async function runCleanupScan() {
  const btn = document.getElementById('btn-scan');
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Scanning…';
  document.getElementById('cleanup-content').innerHTML = '<div class="empty-state"><div class="spinner"></div><div style="margin-top:10px;font-size:12px;color:var(--text-dim)">Scanning library…</div></div>';
  document.getElementById('cleanup-stats').textContent = 'Scanning…';

  try {
    const res = await fetch(`${API}/cleanup/scan`);
    const data = await res.json();
    if (data.error) { alert('Scan error: ' + data.error); return; }
    cleanupData = data;
    document.getElementById('dupe-count-badge').textContent = `(${data.duplicates.length})`;
    document.getElementById('long-count-badge').textContent = `(${data.long_tracks.length})`;
    document.getElementById('cleanup-stats').textContent =
      `Scanned ${data.total_scanned} tracks · ${data.duplicates.length} duplicates · ${data.long_tracks.length} long tracks`;
    renderCleanupList();
  } catch(e) {
    alert('Scan failed: ' + e.message);
  } finally {
    btn.disabled = false; btn.innerHTML = '🔍 Scan';
  }
}

function renderCleanupList() {
  const items = cleanupTab === 'dupes' ? cleanupData.duplicates : cleanupData.long_tracks;
  const list = document.getElementById('cleanup-content');

  if (!items || !items.length) {
    list.innerHTML = `<div class="empty-state"><div class="icon">${cleanupTab === 'dupes' ? '✅' : '✅'}</div>${cleanupTab === 'dupes' ? 'No duplicates found' : 'No long tracks found'}</div>`;
    document.getElementById('btn-delete-selected').disabled = true;
    return;
  }

  list.innerHTML = items.map(t => {
    const dur = t.duration ? `${Math.floor(t.duration/60)}:${String(t.duration%60).padStart(2,'0')}` : '?';
    const meta = cleanupTab === 'dupes'
      ? `${esc(t.artist)} · ${esc(t.album)} · ${t.bitRate||'?'}kbps · kept: ${t.kept_bitrate||'?'}kbps`
      : `${esc(t.artist)} · ${dur} · ${t.bitRate||'?'}kbps`;
    const reasonLabel = cleanupTab === 'dupes' ? 'DUPE' : `${dur}`;
    return `
      <div class="cleanup-item">
        <input type="checkbox" class="cleanup-check" data-id="${t.id}" onchange="updateDeleteBtn()">
        <div class="cleanup-info">
          <div class="cleanup-title">${esc(t.title)}</div>
          <div class="cleanup-meta">${meta}</div>
        </div>
        <span class="cleanup-reason ${cleanupTab === 'dupes' ? 'duplicate' : 'long'}">${reasonLabel}</span>
      </div>
    `;
  }).join('');

  updateDeleteBtn();
}

function updateDeleteBtn() {
  const checked = document.querySelectorAll('.cleanup-check:checked').length;
  const btn = document.getElementById('btn-delete-selected');
  btn.disabled = checked === 0;
  btn.textContent = checked > 0 ? `🗑 Delete ${checked} selected` : '🗑 Delete Selected';
}

function selectAllCleanup() {
  const checks = document.querySelectorAll('.cleanup-check');
  const allChecked = [...checks].every(c => c.checked);
  checks.forEach(c => c.checked = !allChecked);
  updateDeleteBtn();
}

async function deleteSelected() {
  const checked = [...document.querySelectorAll('.cleanup-check:checked')];
  if (!checked.length) return;

  const ids = checked.map(c => c.dataset.id);
  const deleteFiles = document.getElementById('cleanup-delete-files').checked;

  if (!confirm(`Delete ${ids.length} track(s) from ${deleteFiles ? 'disk and ' : ''}Navidrome?`)) return;

  const btn = document.getElementById('btn-delete-selected');
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Deleting…';

  try {
    const res = await fetch(`${API}/cleanup/delete`, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({track_ids: ids, delete_files: deleteFiles}),
    });
    const data = await res.json();
    alert(`Deleted ${data.deleted_files} file(s).${data.failed > 0 ? ` ${data.failed} failed.` : ''} Navidrome will rescan shortly.`);
    // Re-scan to refresh list
    await runCleanupScan();
  } catch(e) {
    alert('Delete failed: ' + e.message);
  } finally {
    btn.disabled = false;
  }
}
'''

content = content.replace('async function init(){', cleanup_js + 'async function init(){')
open('frontend/index.html', 'w').write(content)
print("Frontend patched")
PYEOF

docker compose up -d --build
echo "✅ Cleanup widget added"
