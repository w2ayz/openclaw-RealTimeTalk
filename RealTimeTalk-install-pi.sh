#!/usr/bin/env bash
# RealTimeTalk-install-pi.sh
# One-command deploy of openclaw-realtimetalk as a systemd user service.
# Run once on the Pi; re-run to update or after changing ExecStart flags.
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON="$SKILL_DIR/RealTimeTalk-daemon.py"
SERVICE_NAME="openclaw-realtimetalk"
SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_FILE="$SERVICE_DIR/$SERVICE_NAME.service"
VENV="$HOME/.local/realtimetalk-venv"
PYTHON="$VENV/bin/python"

echo "=== OpenClaw RealTimeTalk installer ==="
echo "Daemon:   $DAEMON"
echo "Service:  $SERVICE_FILE"
echo ""

# ── 1. Python dependencies ────────────────────────────────────────────────────
echo "[1/4] Installing Python dependencies…"
# Use a venv — Raspberry Pi OS Bookworm (PEP 668) blocks pip3 --user installs
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet "sounddevice>=0.4" "websockets>=12" "numpy>=1.20"
echo "      ✓ venv: $VENV"
echo "      ✓ sounddevice  websockets  numpy"

# ── 2. System audio library ───────────────────────────────────────────────────
echo "[2/4] Checking libportaudio2…"
if dpkg -s libportaudio2 >/dev/null 2>&1; then
    echo "      ✓ already installed"
else
    sudo apt-get install -y -q libportaudio2
    echo "      ✓ installed"
fi

# ── 3. Discover audio devices ─────────────────────────────────────────────────
echo "[3/4] Detecting audio devices…"
echo ""
"$PYTHON" "$DAEMON" --list-devices 2>/dev/null || true
echo ""
echo "      Note the index of your USB mic (input) and speaker (output)."
echo "      Edit AUDIO_INPUT / AUDIO_OUTPUT below if the defaults don't work."
echo "      (Leave as 'none' to let sounddevice pick the system default.)"
echo ""

# ── Edit these two lines if auto-detection picks the wrong device ─────────────
AUDIO_INPUT="none"     # e.g. "2"  → adds --input-device 2
AUDIO_OUTPUT="none"    # e.g. "0"  → adds --output-device 0
# ─────────────────────────────────────────────────────────────────────────────

build_exec_start() {
    local cmd="$PYTHON $DAEMON"
    [ "$AUDIO_INPUT"  != "none" ] && cmd="$cmd --input-device $AUDIO_INPUT"
    [ "$AUDIO_OUTPUT" != "none" ] && cmd="$cmd --output-device $AUDIO_OUTPUT"
    echo "$cmd"
}

# ── 4. systemd user service ───────────────────────────────────────────────────
echo "[4/4] Writing systemd service…"
mkdir -p "$SERVICE_DIR"

EXEC_START="$(build_exec_start)"

cat > "$SERVICE_FILE" << UNIT
[Unit]
Description=OpenClaw RealTimeTalk daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$EXEC_START
Restart=no
StandardOutput=journal
StandardError=journal
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
UNIT

echo "      ✓ $SERVICE_FILE"

# ── Enable linger so the service starts at boot without a login session ───────
loginctl enable-linger "$USER"
echo "      ✓ linger enabled for $USER"

systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME"
systemctl --user start  "$SERVICE_NAME"
echo "      ✓ service enabled and started"

echo ""
echo "=== Done ==="
echo ""
echo "  Status:  systemctl --user status $SERVICE_NAME"
echo "  Logs:    journalctl --user -u $SERVICE_NAME -f"
echo "  Stop:    systemctl --user stop $SERVICE_NAME"
echo "  Toggle:  http://<pi-tailscale-ip>:18790/stop"
echo ""
echo "To set a specific audio device later:"
echo "  1. Edit AUDIO_INPUT / AUDIO_OUTPUT near the top of this script"
echo "  2. Re-run: bash $(basename "$0")"
