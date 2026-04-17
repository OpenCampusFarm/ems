#!/bin/bash

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
INTERVAL=${UPDATE_INTERVAL:-60} # seconds

while true; do
    cd "$REPO_DIR"

    BEFORE=$(git rev-parse HEAD)

    if ! git pull; then
        echo "WARNING: git pull failed (network issue or GitHub downtime), skipping this cycle."
        sleep "$INTERVAL"
        continue
    fi

    AFTER=$(git rev-parse HEAD)

    if [ "$BEFORE" != "$AFTER" ]; then
        echo "New changes detected, restarting supervisord..."
        supervisord ctl reload
    else
        echo "No changes, supervisord not restarted."
    fi

    sleep "$INTERVAL"
done
