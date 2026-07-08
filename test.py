python3 << 'PYEOF'
content = open('/opt/muziekapp/spotidrome/backend/app.py').read()

old = '''    if not song_ids:
        log("⚠ No tracks found in Navidrome yet")
        return 0, len(tracks)
    pl_id, created = nd_get_or_create_playlist(playlist_name, cfg)
    nd_subsonic("updatePlaylist", cfg=cfg, playlistId=pl_id, **{"songIdToAdd": song_ids})'''

new = '''    if not song_ids:
        log("⚠ No tracks found in Navidrome yet")
        return 0, len(tracks)
    pl_id, created = nd_get_or_create_playlist(playlist_name, cfg)
    # Send in batches of 50 to avoid 414 URI Too Large
    for i in range(0, len(song_ids), 50):
        batch = song_ids[i:i+50]
        params = {"playlistId": pl_id}
        for j, sid in enumerate(batch):
            params[f"songIdToAdd[{j}]"] = sid
        nd_subsonic("updatePlaylist", cfg=cfg, **params)'''

if old in content:
    open('/opt/muziekapp/spotidrome/backend/app.py', 'w').write(content.replace(old, new))
    print("Patched successfully")
else:
    print("Pattern not found")
PYEOF
