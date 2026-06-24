#!/bin/bash
set -e

cd "$(dirname "$0")"

BEFORE=$(git rev-parse HEAD)
git pull --rebase
AFTER=$(git rev-parse HEAD)

if [ "$BEFORE" = "$AFTER" ]; then
    echo "$(date): No changes, skipping rebuild."
    exit 0
fi

echo "$(date): Changes detected ($BEFORE -> $AFTER), rebuilding..."
docker compose build --no-cache athena-deck
docker compose up -d --build

# Prune dangling images left over from --no-cache rebuilds.
docker image prune -f >/dev/null 2>&1 || true
echo "$(date): Dangling images pruned."

echo "$(date): Update complete."
