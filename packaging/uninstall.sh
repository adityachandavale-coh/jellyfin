#!/bin/sh
# Remove Lumen.
#
#   sudo ./packaging/uninstall.sh            # keeps config, database, users
#   sudo ./packaging/uninstall.sh --purge    # removes those too
#
# Your media files are never touched.

set -eu

PREFIX=/opt/lumen
CONFIG_DIR=/etc/lumen
STATE_DIR=/var/lib/lumen
CACHE_DIR=/var/cache/lumen
UNIT=/etc/systemd/system/lumen.service
SERVICE_USER=lumen

PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

if [ "$(id -u)" -ne 0 ]; then
    echo "This script needs root: sudo $0" >&2
    exit 1
fi

if [ "$PURGE" = "1" ]; then
    printf 'This deletes %s (users, library index) and %s. Continue? [y/N] ' \
        "$STATE_DIR" "$CONFIG_DIR"
    read -r reply
    case "$reply" in
        y|Y|yes|YES) ;;
        *) echo "Aborted."; exit 1 ;;
    esac
fi

if command -v systemctl >/dev/null 2>&1; then
    systemctl stop lumen 2>/dev/null || true
    systemctl disable lumen 2>/dev/null || true
fi

rm -f "$UNIT" /usr/local/bin/lumen
rm -rf "$PREFIX" "$CACHE_DIR"

if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload
fi

if [ "$PURGE" = "1" ]; then
    rm -rf "$STATE_DIR" "$CONFIG_DIR"
    if id "$SERVICE_USER" >/dev/null 2>&1; then
        userdel "$SERVICE_USER" 2>/dev/null || true
    fi
    echo "Lumen removed, including its config and database."
else
    echo "Lumen removed. Kept $CONFIG_DIR and $STATE_DIR."
    echo "Delete them with: sudo rm -rf $CONFIG_DIR $STATE_DIR"
fi
