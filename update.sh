#!/bin/bash

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
INTERVAL=${UPDATE_INTERVAL:-60} # seconds

while true; do
    cd "$REPO_DIR"

    BEFORE=$(git rev-parse HEAD)

    TIMESTAMP=$(date "+%Y-%m-%d %H:%M:%S")

    if ! OUTPUT=$(git pull 2>&1); then
        echo "$TIMESTAMP WARNING: git pull failed (network issue or GitHub downtime), skipping this cycle."
        sleep "$INTERVAL"
        continue
    fi

    echo "$TIMESTAMP $OUTPUT"

    AFTER=$(git rev-parse HEAD)

    if [ "$BEFORE" != "$AFTER" ]; then
        echo "$TIMESTAMP New changes detected, restarting supervisord..."
        sudo systemctl restart ems
    fi

    sleep "$INTERVAL"
done
