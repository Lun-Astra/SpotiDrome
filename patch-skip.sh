#!/bin/bash
set -e
cd /opt/muziekapp/spotidrome

# Get the current app.py from the container
docker cp spotidrome-backend-1:/app/app.py /tmp/current_app.py

# Add remote_file_exists function and fix the skip logic
python3 << 'PYEOF'
content = open('/tmp/current_app.py').read()

# Add helper function after sanitize()
new_func = '''
def remote_file_exists(filename, remote_dir, ssh_cfg):
    """Check if a file starting with filename exists on the remote server."""
    if not ssh_cfg:
        return False
    try:
        cmd = ["ssh", "-i", "/root/.ssh/id_rsa",
               "-p", str(ssh_cfg["port"]),
               "-o", "StrictHostKeyChecking=no",
               "-o", "BatchMode=yes",
               "-o", "ConnectTimeout=5",
               f"{ssh_cfg['user']}@{ssh_cfg['host']}",
               f"ls {remote_dir}/{filename}.mp3 2>/dev/null && echo found || echo notfound"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return "found" in result.stdout
    except Exception:
        return False

'''

# Insert after sanitize function
content = content.replace(
    'def download_worker(',
    new_func + 'def download_worker('
)

# Fix the skip check inside download_worker
old_skip = '''        if [f for f in os.listdir(local_playlist_dir) if f.startswith(filename)]:
            with job_lock:
                jobs[job_id]["log"].append(f"⏭ Skipped (exists): {track['artist']} - {track['name']}")
            downloaded_tracks.append(track)
            continue'''

new_skip = '''        # Check locally first, then on remote
        local_exists = bool([f for f in os.listdir(local_playlist_dir) if f.startswith(filename)])
        remote_exists = remote_file_exists(
            filename,
            os.path.join(load_ssh_config()["music_path"], sanitize(playlist_name)) if load_ssh_config() else "",
            load_ssh_config()
        ) if not local_exists else False

        if local_exists or remote_exists:
            with job_lock:
                where = "local" if local_exists else "remote"
                jobs[job_id]["log"].append(f"⏭ Skipped (exists on {where}): {track['artist']} - {track['name']}")
            downloaded_tracks.append(track)
            continue'''

content = content.replace(old_skip, new_skip)

open('/tmp/patched_app.py', 'w').write(content)
print("Patched successfully")
PYEOF

# Copy patched file into container
docker cp /tmp/patched_app.py spotidrome-backend-1:/app/app.py
docker restart spotidrome-backend-1
echo "✅ Backend patched and restarted — remote file check enabled"
