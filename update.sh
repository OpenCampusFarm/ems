#!/bin/bash

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
INTERVAL=${UPDATE_INTERVAL:-60} # seconds

while true; do
    cd "$REPO_DIR"

    BEFORE=$(git rev-parse HEAD)
    git pull
    AFTER=$(git rev-parse HEAD)

    if [ "$BEFORE" != "$AFTER" ]; then
        echo "New changes detected, restarting supervisord..."
        supervisorctl reload
    else
        echo "No changes, supervisord not restarted."
    fi

    sleep "$INTERVAL"
done
