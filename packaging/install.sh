#!/bin/sh
# Install Lumen as a systemd service.
#
#   sudo ./packaging/install.sh
#
# Safe to re-run: it upgrades the code in place and leaves your config,
# database and users alone. Uninstall with ./packaging/uninstall.sh

set -eu

PREFIX=/opt/lumen
CONFIG_DIR=/etc/lumen
STATE_DIR=/var/lib/lumen
CACHE_DIR=/var/cache/lumen
UNIT=/etc/systemd/system/lumen.service
SERVICE_USER=lumen

SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

if [ "$(id -u)" -ne 0 ]; then
    echo "This script needs root: sudo $0" >&2
    exit 1
fi

# --- prerequisites ---------------------------------------------------

PYTHON=$(command -v python3 || true)
if [ -z "$PYTHON" ]; then
    echo "python3 is required but was not found." >&2
    echo "Install it with your package manager, e.g. apt install python3" >&2
    exit 1
fi

"$PYTHON" - <<'PYCHECK' || exit 1
import sys
if sys.version_info < (3, 9):
    sys.exit("Lumen needs Python 3.9 or newer; found %d.%d" % sys.version_info[:2])
PYCHECK

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "WARNING: ffmpeg was not found."
    echo "  Files the browser can play directly will still work, but nothing"
    echo "  else will. Install it with e.g. 'apt install ffmpeg' and restart."
    echo
fi

if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemctl not found — this script targets systemd systems." >&2
    exit 1
fi

# --- user and directories ---------------------------------------------

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    echo "Creating system user '$SERVICE_USER'"
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER" \
        || useradd --system --no-create-home --shell /sbin/nologin "$SERVICE_USER"
fi

install -d -m 0755 "$PREFIX"
install -d -m 0755 "$CONFIG_DIR"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$STATE_DIR"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$CACHE_DIR"

# --- code ---------------------------------------------------------------

echo "Installing the package into $PREFIX"
rm -rf "$PREFIX/lumen"
cp -r "$SOURCE_DIR/lumen" "$PREFIX/lumen"
find "$PREFIX/lumen" -type d -exec chmod 0755 {} +
find "$PREFIX/lumen" -type f -exec chmod 0644 {} +

# A convenience wrapper so 'lumen users', 'lumen useradd' etc. just work.
# PYTHONPATH is how it finds the package from outside $PREFIX.
cat > /usr/local/bin/lumen <<EOF
#!/bin/sh
export PYTHONPATH="$PREFIX"
exec "$PYTHON" -m lumen --config "$CONFIG_DIR/config.ini" "\$@"
EOF
chmod 0755 /usr/local/bin/lumen

# --- config ---------------------------------------------------------------

if [ ! -f "$CONFIG_DIR/config.ini" ]; then
    echo "Writing a starter config to $CONFIG_DIR/config.ini"
    cp "$SOURCE_DIR/packaging/config.example.ini" "$CONFIG_DIR/config.ini"
    chmod 0644 "$CONFIG_DIR/config.ini"
    NEW_CONFIG=1
else
    echo "Keeping the existing $CONFIG_DIR/config.ini"
    NEW_CONFIG=0
fi

# --- service ---------------------------------------------------------------

echo "Installing the systemd unit"
cp "$SOURCE_DIR/packaging/lumen.service" "$UNIT"
chmod 0644 "$UNIT"
systemctl daemon-reload
systemctl enable lumen >/dev/null

echo
echo "Installed."
echo

if [ "$NEW_CONFIG" = "1" ]; then
    cat <<EOF
Next steps:

  1. Point Lumen at your media:
       sudoedit $CONFIG_DIR/config.ini      # set library.media_dirs

  2. Make sure the service can read it, and that systemd allows it:
       sudo chmod o+rx /path/to/your/media
       sudoedit $UNIT                       # set ReadOnlyPaths= to your dirs
       sudo systemctl daemon-reload

  3. Start it:
       sudo systemctl start lumen
       lumen check                          # confirms config, ffmpeg, paths

  4. Open http://<this-host>:8096 and create the first account in the browser
     (or: sudo -u $SERVICE_USER lumen useradd yourname --admin)

EOF
else
    echo "Restart to pick up the new code:  sudo systemctl restart lumen"
    echo
fi

echo "Logs:  journalctl -u lumen -f"
