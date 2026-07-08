#!/bin/bash
set -e
cd /opt/muziekapp/spotidrome

# ── 1. Add schedule API endpoints to backend ──────────────────────────────────
python3 << 'PYEOF'
content = open('backend/app.py').read()

# Add schedule config file constant if not present
if 'SCHEDULE_CONFIG_FILE' not in content:
    content = content.replace(
        'TRACKED_FILE          = "/root/.ssh/tracked_playlists.json"',
        'TRACKED_FILE          = "/root/.ssh/tracked_playlists.json"\nSCHEDULE_CONFIG_FILE  = "/root/.ssh/schedule_config.json"'
    )

# Add schedule load/save functions before scheduler_loop
schedule_funcs = '''
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

'''

if 'def load_schedule_config' not in content:
    content = content.replace('def scheduler_loop():', schedule_funcs + 'def scheduler_loop():')

# Replace scheduler_loop to use config
old_scheduler = '''def scheduler_loop():
    while True:
        now = datetime.utcnow()
        from datetime import timedelta
        target = now.replace(hour=3, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        wait_seconds = (target - now).total_seconds()
        print(f"[scheduler] Next auto-sync in {wait_seconds/3600:.1f} hours")
        time.sleep(wait_seconds)
        threading.Thread(target=auto_sync_worker, daemon=True).start()'''

new_scheduler = '''def scheduler_loop():
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
            threading.Thread(target=auto_sync_worker, daemon=True).start()'''

if old_scheduler in content:
    content = content.replace(old_scheduler, new_scheduler)
else:
    print("WARNING: scheduler_loop pattern not found, skipping")

# Add schedule routes before if __name__
schedule_routes = '''
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

'''

if '@app.route("/schedule"' not in content:
    content = content.replace("if __name__ == \"__main__\":", schedule_routes + "if __name__ == \"__main__\":")

open('backend/app.py', 'w').write(content)
print("Backend patched with schedule API")
PYEOF

# ── 2. Add schedule UI to frontend ────────────────────────────────────────────
python3 << 'PYEOF'
content = open('frontend/index.html').read()

# Add CSS for schedule modal
schedule_css = '''
  /* Schedule modal extras */
  .schedule-row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  .radio-group { display: flex; gap: 12px; flex-wrap: wrap; }
  .radio-opt {
    display: flex; align-items: center; gap: 8px; cursor: pointer;
    background: var(--surface2); border: 1px solid var(--border);
    padding: 8px 14px; border-radius: 6px; font-size: 12px;
    transition: border-color .15s;
  }
  .radio-opt:has(input:checked) { border-color: var(--nd); color: var(--nd); }
  .radio-opt input { accent-color: var(--nd); }
  .next-run {
    background: rgba(108,99,255,.08); border: 1px solid rgba(108,99,255,.2);
    border-radius: 6px; padding: 10px 14px; font-size: 12px; color: var(--nd);
  }
  .select-input {
    background: var(--surface2); border: 1px solid var(--border); border-radius: 6px;
    color: var(--text); font-family: var(--font-mono); font-size: 13px;
    padding: 8px 12px; outline: none; transition: border-color .15s;
  }
  .select-input:focus { border-color: var(--nd); }
  .pill.schedule:hover { border-color: var(--accent); }
  .dot.on-schedule { background: var(--accent); box-shadow: 0 0 5px var(--accent); }
'''

content = content.replace(
    '#callback-overlay.show { display: flex; }',
    '#callback-overlay.show { display: flex; }' + schedule_css
)

# Add schedule pill to header
content = content.replace(
    '<div class="nd-pill" onclick="openNdModal()" id="nd-pill">',
    '<div class="pill schedule" onclick="openScheduleModal()" id="schedule-pill"><div class="dot" id="schedule-dot"></div><span id="schedule-pill-label">Auto-sync</span></div>\n      <div class="nd-pill" onclick="openNdModal()" id="nd-pill">'
)

# Add schedule modal HTML before closing body
schedule_modal = '''
<div class="modal-overlay" id="schedule-modal">
  <div class="modal">
    <div class="modal-header">
      <div class="modal-title" style="color:var(--accent)">⏰ Auto-sync Schedule</div>
      <button class="modal-close" onclick="closeScheduleModal()">✕</button>
    </div>
    <div class="modal-body">
      <div class="field-hint">Automatically re-sync all tracked playlists on a schedule. Only downloads new tracks added since last sync.</div>

      <div class="field">
        <label>Status</label>
        <label class="toggle-wrap">
          <label class="toggle"><input type="checkbox" id="schedule-enabled" checked><span class="toggle-slider"></span></label>
          <span id="schedule-enabled-label" class="sync-label">Enabled</span>
        </label>
      </div>

      <div class="field">
        <label>Schedule Type</label>
        <div class="radio-group">
          <label class="radio-opt">
            <input type="radio" name="schedule-mode" value="time" checked> Fixed time (daily)
          </label>
          <label class="radio-opt">
            <input type="radio" name="schedule-mode" value="interval"> Every X hours
          </label>
        </div>
      </div>

      <div class="field" id="field-time">
        <label>Time (UTC)</label>
        <select class="select-input" id="schedule-hour">
          ${Array.from({length:24},(_,i)=>`<option value="${i}">${String(i).padStart(2,'0')}:00</option>`).join('')}
        </select>
      </div>

      <div class="field" id="field-interval" style="display:none">
        <label>Interval</label>
        <select class="select-input" id="schedule-interval">
          <option value="1">Every 1 hour</option>
          <option value="2">Every 2 hours</option>
          <option value="4">Every 4 hours</option>
          <option value="6">Every 6 hours</option>
          <option value="12">Every 12 hours</option>
          <option value="24" selected>Every 24 hours</option>
          <option value="48">Every 48 hours</option>
        </select>
      </div>

      <div class="next-run" id="next-run-display">Loading…</div>
    </div>
    <div class="modal-footer">
      <button class="btn btn-outline" onclick="runSyncNow()">▶ Run Now</button>
      <button class="btn btn-primary" style="background:var(--nd);color:#fff" onclick="saveSchedule()">Save</button>
    </div>
  </div>
</div>
'''

content = content.replace('<script>', schedule_modal + '<script>')

# Add schedule JS functions
schedule_js = '''
// ── Schedule ─────────────────────────────────────────────────────────────────
async function checkSchedule() {
  try {
    const res = await fetch(`${API}/schedule`);
    const cfg = await res.json();
    const dot = document.getElementById('schedule-dot');
    const label = document.getElementById('schedule-pill-label');
    if (cfg.enabled) {
      dot.classList.add('on-schedule');
      label.textContent = cfg.mode === 'interval' ? `Every ${cfg.interval_hours}h` : `Daily ${String(cfg.hour).padStart(2,'0')}:00`;
    } else {
      dot.classList.remove('on-schedule');
      label.textContent = 'Auto-sync off';
    }
  } catch(e) {}
}

function openScheduleModal() {
  fetch(`${API}/schedule`).then(r=>r.json()).then(cfg => {
    document.getElementById('schedule-enabled').checked = cfg.enabled;
    document.getElementById('schedule-enabled-label').textContent = cfg.enabled ? 'Enabled' : 'Disabled';
    document.getElementById('schedule-hour').value = cfg.hour || 3;
    document.getElementById('schedule-interval').value = cfg.interval_hours || 24;
    document.querySelectorAll('input[name="schedule-mode"]').forEach(r => {
      r.checked = r.value === (cfg.mode || 'time');
    });
    toggleScheduleMode(cfg.mode || 'time');
    document.getElementById('next-run-display').textContent = cfg.next_run ? `⏰ Next sync: ${cfg.next_run}` : 'Auto-sync is disabled';
  });
  document.getElementById('schedule-modal').classList.add('show');
}

function closeScheduleModal() { document.getElementById('schedule-modal').classList.remove('show'); }

function toggleScheduleMode(mode) {
  document.getElementById('field-time').style.display = mode === 'time' ? 'flex' : 'none';
  document.getElementById('field-interval').style.display = mode === 'interval' ? 'flex' : 'none';
}

document.querySelectorAll('input[name="schedule-mode"]').forEach(r => {
  r.addEventListener('change', () => toggleScheduleMode(r.value));
});

document.getElementById('schedule-enabled').addEventListener('change', function() {
  document.getElementById('schedule-enabled-label').textContent = this.checked ? 'Enabled' : 'Disabled';
});

async function saveSchedule() {
  const mode = document.querySelector('input[name="schedule-mode"]:checked').value;
  const payload = {
    enabled: document.getElementById('schedule-enabled').checked,
    mode,
    hour: parseInt(document.getElementById('schedule-hour').value),
    interval_hours: parseInt(document.getElementById('schedule-interval').value),
  };
  const res = await fetch(`${API}/schedule`, {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify(payload),
  });
  await res.json();
  checkSchedule();
  closeScheduleModal();
}

async function runSyncNow() {
  await fetch(`${API}/schedule/run`, { method: 'POST' });
  closeScheduleModal();
  setTimeout(renderJobs, 1000);
}

'''

content = content.replace(
    "async function init(){",
    schedule_js + "async function init(){"
)

# Call checkSchedule in init
content = content.replace(
    "await checkNd();",
    "await checkNd();\n  await checkSchedule();"
)

open('frontend/index.html', 'w').write(content)
print("Frontend patched with schedule UI")
PYEOF

# ── 3. Rebuild ────────────────────────────────────────────────────────────────
docker compose up -d --build
echo "✅ Auto-sync UI added and rebuilt"
