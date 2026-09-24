#!/bin/sh
set -e

# Sessions, SQLite state and logs must survive restarts/redeploys.
# Render mounts the persistent disk at /var/data.
DISK="${PERSIST_DIR:-/var/data}"

mkdir -p "$DISK/telegram_sessions" "$DISK/data" "$DISK/logs"

rm -rf /app/telegram_sessions /app/data /app/logs
ln -s "$DISK/telegram_sessions" /app/telegram_sessions
ln -s "$DISK/data" /app/data
ln -s "$DISK/logs" /app/logs

exec python main.py
