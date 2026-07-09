#!/usr/bin/env python3
"""Find Navidrome playlists that share the same name and merge them into one.

Dry-run by default — prints the merge plan without changing anything.
Pass --apply to actually add the missing tracks to the kept playlist and
delete the duplicates.

Usage (from the backend container, which already has the Navidrome
credentials mounted at /root/.ssh/navidrome_config.json):

    docker compose exec backend python3 scripts/merge_duplicate_playlists.py
    docker compose exec backend python3 scripts/merge_duplicate_playlists.py --apply
"""
import argparse
import json
import sys

import requests

DEFAULT_CONFIG = "/root/.ssh/navidrome_config.json"
BATCH_SIZE = 50


def load_config(path):
    with open(path) as f:
        return json.load(f)


def subsonic(cfg, action, **params):
    defaults = {"u": cfg["user"], "p": cfg["password"], "v": "1.16.1", "c": "spotidrome-merge", "f": "json"}
    defaults.update(params)
    resp = requests.get(f"{cfg['url']}/rest/{action}", params=defaults, timeout=30)
    resp.raise_for_status()
    data = resp.json().get("subsonic-response", {})
    if data.get("status") != "ok":
        raise RuntimeError(f"Subsonic error on {action}: {data.get('error', {}).get('message', 'unknown')}")
    return data


def get_playlists(cfg):
    data = subsonic(cfg, "getPlaylists")
    return data.get("playlists", {}).get("playlist", []) or []


def get_playlist_songs(cfg, playlist_id):
    data = subsonic(cfg, "getPlaylist", id=playlist_id)
    return data.get("playlist", {}).get("entry", []) or []


def add_songs(cfg, playlist_id, song_ids):
    for i in range(0, len(song_ids), BATCH_SIZE):
        batch = song_ids[i:i + BATCH_SIZE]
        subsonic(cfg, "updatePlaylist", playlistId=playlist_id, songIdToAdd=batch)


def delete_playlist(cfg, playlist_id):
    subsonic(cfg, "deletePlaylist", id=playlist_id)


def find_duplicate_groups(playlists):
    groups = {}
    for pl in playlists:
        key = pl["name"].strip().lower()
        groups.setdefault(key, []).append(pl)
    return {k: v for k, v in groups.items() if len(v) > 1}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Actually merge and delete. Without this, only prints the plan.")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help=f"Path to Navidrome config JSON (default: {DEFAULT_CONFIG})")
    args = parser.parse_args()

    cfg = load_config(args.config)
    playlists = get_playlists(cfg)
    dupes = find_duplicate_groups(playlists)

    if not dupes:
        print("No duplicate playlists found.")
        return

    total_deleted = 0
    for group in dupes.values():
        # Keep the oldest playlist so its id (and any external references) survive.
        group.sort(key=lambda p: p.get("created", ""))
        primary, others = group[0], group[1:]

        print(f"\n=== '{primary['name']}' — {len(group)} copies ===")
        print(f"  keeping: id={primary['id']} created={primary.get('created')} songCount={primary.get('songCount')}")

        seen_ids = {s["id"] for s in get_playlist_songs(cfg, primary["id"])}
        to_add = []
        for dup in others:
            dup_songs = get_playlist_songs(cfg, dup["id"])
            new_ids = [s["id"] for s in dup_songs if s["id"] not in seen_ids]
            seen_ids.update(new_ids)
            to_add.extend(new_ids)
            print(f"  merging: id={dup['id']} created={dup.get('created')} songCount={dup.get('songCount')} (+{len(new_ids)} new track(s))")

        print(f"  -> add {len(to_add)} new track(s) to '{primary['name']}', then delete {len(others)} duplicate(s)")

        if args.apply:
            if to_add:
                add_songs(cfg, primary["id"], to_add)
            for dup in others:
                delete_playlist(cfg, dup["id"])
                total_deleted += 1

    if args.apply:
        print(f"\nDone. Deleted {total_deleted} duplicate playlist(s).")
    else:
        print("\nDry run only — re-run with --apply to perform the merge.")


if __name__ == "__main__":
    sys.exit(main())
