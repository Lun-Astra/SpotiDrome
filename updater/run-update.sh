#!/bin/sh
# One SpotiDrome update, started by updater.sh in its own container: the code
# (when this is a git checkout), then the newest images, then restart. Progress
# goes to update.log / update_status.json in the state folder, which the
# backend shows in Settings.
set -u

LOG=/state/update.log
STATUS=/state/update_status.json

log() { echo "[$(date -u +%H:%M:%S)] $*" >> "$LOG"; }
status() {
    printf '{"state": "%s", "at": %s, "message": "%s"}\n' "$1" "$(date +%s)" "$2" > "$STATUS.tmp"
    mv "$STATUS.tmp" "$STATUS"
}
run() {
    log "\$ $*"
    "$@" >> "$LOG" 2>&1
}

: > "$LOG"
status running "Updating…"
log "Updating SpotiDrome (project $PROJECT in $PWD)"

# 1. The code - docker-compose.yml and these scripts. Run as the checkout's owner,
#    so the files don't end up belonging to root. Never fatal: a checkout with
#    changes of its own just keeps them, the images below still update.
if [ -d .git ]; then
    owner=$(stat -c %u:%g .git)
    if run docker run --rm --user "$owner" -e HOME=/tmp -v "$PWD:$PWD" -w "$PWD" \
            alpine/git:latest -c safe.directory='*' pull --ff-only; then
        log "Code is up to date"
    else
        log "Couldn't update the code with git pull (local changes?) - continuing with the images"
    fi
fi

# 2. The newest images, then recreate whatever changed. This also replaces
#    the updater itself when its image changed - this container goes on.
COMPOSE="docker compose -p $PROJECT"
if ! run $COMPOSE pull; then
    log "Downloading the new images failed"
    status failed "Downloading the new images failed - see the log"
    exit 1
fi
if ! run $COMPOSE up -d --remove-orphans; then
    log "Restarting SpotiDrome failed"
    status failed "Restarting SpotiDrome failed - see the log"
    exit 1
fi
run docker image prune -f
log "Done"
status done "Updated"
