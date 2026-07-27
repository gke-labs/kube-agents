#!/bin/sh
set -e

TARGET_DIR="${1:-/opt/data}"

if [ -d /etc/agent-extensions-raw ]; then
	for f in /etc/agent-extensions-raw/*; do
		if [ -f "$f" ]; then
			rel=$(basename "$f" | sed "s/___/\//g")
			dir=$(dirname "$TARGET_DIR/$rel")
			mkdir -p "$dir"
			cp "$f" "$TARGET_DIR/$rel"
			chmod 644 "$TARGET_DIR/$rel"
		fi
	done
fi
