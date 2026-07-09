#!/usr/bin/env python3
"""Find albums that Navidrome split into multiple records because tracks had
inconsistent ALBUMARTIST tags (e.g. a soundtrack where each song credits a
different combination of vocalists), and retag the underlying files with a
single consistent album artist so Navidrome merges them back into one album
on the next scan.

For each group of same-named album fragments:
  - If one fragment holds a strict majority of the group's tracks, its
    existing album-artist tag is trusted and used for the whole group.
  - Otherwise the group is tagged "Various Artists" (the standard tag for a
    genuine multi-artist compilation).

Placeholder album names ("", "unknown album", "[unknown album]") are always
skipped — those are unrelated tracks that just share a generic name, not a
single fragmented album.

Dry-run by default — prints the merge plan without changing anything.
Pass --apply to actually retag files on the remote host and trigger a scan.

Must run where the Navidrome SSH credentials are already available, e.g.:

    docker compose exec backend python3 scripts/retag_fragmented_albums.py
    docker compose exec backend python3 scripts/retag_fragmented_albums.py --apply
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections import defaultdict

import requests
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TPE2, error as ID3Error

NAVIDROME_CONFIG_FILE = "/root/.ssh/navidrome_config.json"
SSH_CONFIG_FILE = "/root/.ssh/ssh_config.json"
PLACEHOLDER_NAMES = {"", "unknown album", "[unknown album]"}
MAJORITY_THRESHOLD = 0.5


def load_nd_config():
    with open(NAVIDROME_CONFIG_FILE) as f:
        return json.load(f)


def load_ssh_config():
    host = os.environ.get("SSH_HOST", "")
    user = os.environ.get("SSH_USER", "")
    port = os.environ.get("SSH_PORT", "22")
    path = os.environ.get("SSH_MUSIC_PATH", "")
    if host and user:
        return {"host": host, "user": user, "port": int(port), "music_path": path}
    with open(SSH_CONFIG_FILE) as f:
        return json.load(f)


def nd_login(cfg):
    resp = requests.post(f"{cfg['url']}/auth/login",
                          json={"username": cfg["user"], "password": cfg["password"]}, timeout=15)
    resp.raise_for_status()
    return resp.json()["token"]


def nd_get(cfg, token, path, params=None):
    resp = requests.get(f"{cfg['url']}{path}", headers={"x-nd-authorization": f"Bearer {token}"},
                         params=params or {}, timeout=30)
    resp.raise_for_status()
    return resp


def find_fragment_groups(albums):
    groups = defaultdict(list)
    for a in albums:
        key = a.get("name", "").strip().lower()
        if key in PLACEHOLDER_NAMES:
            continue
        groups[key].append(a)
    return {k: v for k, v in groups.items() if len(v) > 1}


def canonical_album_artist(group):
    total = sum(a.get("songCount", 0) for a in group)
    largest = max(group, key=lambda a: a.get("songCount", 0))
    if total > 0 and largest.get("songCount", 0) / total > MAJORITY_THRESHOLD:
        return largest.get("albumArtist") or "Various Artists"
    return "Various Artists"


def retag_file(local_path, album_artist):
    if local_path.endswith(".flac"):
        tags = FLAC(local_path)
        tags["albumartist"] = [album_artist]
        tags.save()
    else:
        try:
            tags = ID3(local_path)
        except ID3Error:
            tags = ID3()
        tags["TPE2"] = TPE2(encoding=3, text=album_artist)
        tags.save(local_path)


def rsync(ssh_cfg, src, dst):
    cmd = ["rsync", "-a",
           "-e", f"ssh -i /root/.ssh/id_rsa -p {ssh_cfg['port']} -o StrictHostKeyChecking=no -o BatchMode=yes",
           src, dst]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return result.returncode == 0, result.stderr[-300:]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Actually retag files and rescan. Without this, only prints the plan.")
    parser.add_argument("--skip", default="", help="Comma-separated album names to exclude (case-insensitive) — use for same-title-different-album false positives.")
    args = parser.parse_args()
    skip_names = {s.strip().lower() for s in args.skip.split(",") if s.strip()}

    nd_cfg = load_nd_config()
    ssh_cfg = load_ssh_config()
    token = nd_login(nd_cfg)

    albums = nd_get(nd_cfg, token, "/api/album", {"_end": 5000}).json()
    groups = find_fragment_groups(albums)
    print(f"Total albums: {len(albums)}  |  fragmented groups: {len(groups)}\n")

    skipped = [g[0]["name"] for g in groups.values() if g[0]["name"].strip().lower() in skip_names]
    if skipped:
        print(f"Skipping {len(skipped)} group(s) per --skip: {', '.join(skipped)}\n")

    plan = []
    for group in groups.values():
        if group[0]["name"].strip().lower() in skip_names:
            continue
        target_artist = canonical_album_artist(group)
        songs = []
        for a in group:
            page = nd_get(nd_cfg, token, "/api/song", {"album_id": a["id"], "_end": 1000}).json()
            for s in page:
                if (s.get("albumArtist") or "") != target_artist:
                    songs.append(s)
        if songs:
            plan.append((group[0]["name"], target_artist, group, songs))

    total_files = sum(len(s) for _, _, _, s in plan)
    print(f"Groups needing retag: {len(plan)}  |  files to retag: {total_files}\n")
    for name, target_artist, group, songs in plan:
        print(f"=== {name!r} -> albumartist={target_artist!r} ({len(songs)} file(s) of {sum(g.get('songCount',0) for g in group)}) ===")
        for a in group:
            print(f"  fragment id={a['id']} artist={a.get('albumArtist')!r} songCount={a.get('songCount')}")

    if not args.apply:
        print("\nDry run only — re-run with --apply to retag files and trigger a rescan.")
        return

    print()
    ok_count, fail_count = 0, 0
    with tempfile.TemporaryDirectory() as tmp:
        for name, target_artist, group, songs in plan:
            for s in songs:
                rel_path = s["path"]
                remote_path = f"{s.get('libraryPath') or ssh_cfg['music_path']}/{rel_path}"
                local_path = os.path.join(tmp, os.path.basename(rel_path))
                remote = f"{ssh_cfg['user']}@{ssh_cfg['host']}:{remote_path}"

                ok, err = rsync(ssh_cfg, remote, local_path)
                if not ok:
                    print(f"  ✗ download failed: {rel_path} ({err})")
                    fail_count += 1
                    continue
                try:
                    retag_file(local_path, target_artist)
                except Exception as e:
                    print(f"  ✗ retag failed: {rel_path} ({e})")
                    fail_count += 1
                    continue
                ok, err = rsync(ssh_cfg, local_path, remote)
                os.remove(local_path)
                if not ok:
                    print(f"  ✗ upload failed: {rel_path} ({err})")
                    fail_count += 1
                    continue
                ok_count += 1
                print(f"  ✓ retagged: {rel_path}")

    print(f"\nRetagged {ok_count} file(s), {fail_count} failure(s).")

    print("Triggering Navidrome full rescan…")
    resp = requests.put(f"{nd_cfg['url']}/api/scanner/trigger", auth=(nd_cfg["user"], nd_cfg["password"]),
                         params={"fullScan": "true"}, timeout=15)
    print("Scan trigger status:", resp.status_code)


if __name__ == "__main__":
    sys.exit(main())
