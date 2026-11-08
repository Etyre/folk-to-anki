#!/bin/bash
# Installs (or reinstalls) the launchd agent that runs folk-to-anki a few times a day.
set -euo pipefail
cd "$(dirname "$0")"
LABEL=com.elityre.folk-to-anki
DEST=~/Library/LaunchAgents/$LABEL.plist
[ -f config.env ] || { echo "config.env missing: copy config.env.example and fill it in"; exit 1; }
mkdir -p ~/Library/Logs "$HOME/Library/Application Support/FolkToAnki/photos"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
cp "$LABEL.plist" "$DEST"
launchctl bootstrap "gui/$(id -u)" "$DEST"
echo "installed $LABEL; log: ~/Library/Logs/folk-to-anki.log"
