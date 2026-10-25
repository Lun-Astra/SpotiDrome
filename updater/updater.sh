#!/bin/sh
# SpotiDrome's updater (the "updater" service in docker-compose.yml).
#
# The backend can be reached from the internet, so it never gets the Docker
# socket. Settings > Update now only makes it write update_request.json into
# the shared state folder (~/.ssh); this container - the only one with the
# socket, and not on any network port - notices that and starts the update.
#
# The update itself runs in a separate one-off container (run-update.sh), so
# it keeps going while `docker compose up -d` recreates this one.
set -u

STATE=/state
REQUEST=$STATE/update_request.json
HEARTBEAT=$STATE/updater_heartbeat

SELF=$(hostname)
inspect() { docker inspect "$SELF" --format "$1"; }

PROJECT=$(inspect '{{index .Config.Labels "com.docker.compose.project"}}')
WORKDIR=$(inspect '{{index .Config.Labels "com.docker.compose.project.working_dir"}}')
IMAGE=$(inspect '{{.Config.Image}}')
STATE_SOURCE=$(inspect '{{range .Mounts}}{{if eq .Destination "/state"}}{{.Source}}{{end}}{{end}}')
RUNNER="${PROJECT}-update-run"

echo "[updater] watching $REQUEST for project '$PROJECT' in $WORKDIR"

while true; do
    date +%s > "$HEARTBEAT"
    if [ -f "$REQUEST" ]; then
        rm -f "$REQUEST"
        if [ -n "$(docker ps -q --filter "name=^${RUNNER}$")" ]; then
            echo "[updater] an update is already running"
        else
            echo "[updater] update requested - starting $RUNNER"
            docker rm -f "$RUNNER" >/dev/null 2>&1
            # Same paths as on the host, so docker-compose.yml's relative paths, .env
            # and ~ (HOME) mean exactly what they mean when you run compose yourself.
            docker run -d --rm --name "$RUNNER" \
                -v /var/run/docker.sock:/var/run/docker.sock \
                -v "$WORKDIR:$WORKDIR" -w "$WORKDIR" \
                -v "$STATE_SOURCE:/state" \
                -e HOME="${HOST_HOME:-/root}" -e PROJECT="$PROJECT" \
                --entrypoint sh "$IMAGE" "$WORKDIR/updater/run-update.sh" >/dev/null \
                || echo "[updater] couldn't start the update container"
        fi
    fi
    sleep 5
done
