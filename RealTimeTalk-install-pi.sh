#!/usr/bin/env bash
# RealTimeTalk-install-pi.sh
# One-command deploy of openclaw-realtimetalk as a systemd user service.
# Run once on a new Pi; safe to re-run any time (every step checks first and
# skips what's already in place) — re-run after `git pull` or to fix a
# half-finished install.
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON="$SKILL_DIR/RealTimeTalk-daemon.py"
SERVICE_NAME="openclaw-realtimetalk"
SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_FILE="$SERVICE_DIR/$SERVICE_NAME.service"
VENV="$HOME/.local/realtimetalk-venv"
PYTHON="$VENV/bin/python"
PIPER_DIR="$HOME/.local/bin/piper-native"
PIPER_BIN="$PIPER_DIR/piper"
VOICES_DIR="$HOME/.local/share/piper/voices"
SPEAKER_MODEL_DIR="$HOME/.local/share/rtt/speaker"
SPEAKER_MODEL="$SPEAKER_MODEL_DIR/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"
OPENCLAW_CONFIG="$HOME/.openclaw/openclaw.json"
PIPER_RELEASE="2023.11.14-2"

echo "=== OpenClaw RealTimeTalk installer ==="
echo "Daemon:   $DAEMON"
echo "Service:  $SERVICE_FILE"
echo ""

# ── 1. System packages ────────────────────────────────────────────────────────
echo "[1/8] Checking system packages…"
# libportaudio2   - PortAudio (sounddevice's native backend)
# pulseaudio-utils - provides `pactl`, used throughout for PipeWire sink/source control
# pipewire-alsa   - ALSA-compatibility plugin; without it, ALSA's "default"/"pipewire"
#                   pseudo-devices report 0 input/output channels and raw hw:X access
#                   can't resample to the rates OpenAI's Realtime API and openwakeword
#                   need (24kHz / 16kHz) — this is what makes mic capture work at all
# espeak-ng       - phonemizer Piper needs for Chinese TTS
# fonts-noto-color-emoji - dashboard button icons include an astral-plane emoji (owner
#                   icon); without a color-emoji font it renders as a blank box
# sox, multimon-ng - only exercised if a radio interface (AIOC/Digirig) is plugged in,
#                   but the DTMF listener thread runs unconditionally and probes for one
#                   every few seconds regardless of whether this Pi has radio hardware —
#                   installed unconditionally so plugging one in later doesn't crash it
REQUIRED_APT_PKGS=(libportaudio2 pulseaudio-utils pipewire-alsa espeak-ng fonts-noto-color-emoji sox multimon-ng)
MISSING_PKGS=()
for pkg in "${REQUIRED_APT_PKGS[@]}"; do
    dpkg -s "$pkg" >/dev/null 2>&1 || MISSING_PKGS+=("$pkg")
done
if [ ${#MISSING_PKGS[@]} -gt 0 ]; then
    echo "      Installing: ${MISSING_PKGS[*]}"
    sudo apt-get update -qq
    sudo apt-get install -y -q "${MISSING_PKGS[@]}"
else
    echo "      ✓ already installed: ${REQUIRED_APT_PKGS[*]}"
fi

# ── 2. Python dependencies ────────────────────────────────────────────────────
echo "[2/8] Installing Python dependencies…"
# Use a venv — Raspberry Pi OS Bookworm (PEP 668) blocks pip3 --user installs
python3 -m venv "$VENV"
"$PYTHON" -m pip install --quiet --upgrade pip
"$PYTHON" -m pip install --quiet -r "$SKILL_DIR/requirements.txt"
echo "      ✓ venv: $VENV"
echo "      ✓ sounddevice websockets numpy sherpa-onnx pyserial openwakeword onnxruntime"

if "$PYTHON" -c "
import openwakeword, os, sys
p = os.path.join(os.path.dirname(openwakeword.__file__), 'resources', 'models', 'hey_jarvis_v0.1.onnx')
sys.exit(0 if os.path.exists(p) else 1)
" 2>/dev/null; then
    echo "      ✓ openwakeword 'hey_jarvis' model present (bundled with the package)"
else
    echo "      ⚠ openwakeword installed but hey_jarvis_v0.1.onnx model missing —"
    echo "        local wake word ('Hey Jarvis') will be disabled; cloud STT still works"
fi

# ── 3. Piper TTS (native binary + voices) ─────────────────────────────────────
echo "[3/8] Checking Piper TTS…"
ARCH="$(uname -m)"
case "$ARCH" in
    aarch64) PIPER_ASSET="piper_linux_aarch64.tar.gz" ;;
    x86_64)  PIPER_ASSET="piper_linux_x86_64.tar.gz" ;;
    armv7l)  PIPER_ASSET="piper_linux_armv7l.tar.gz" ;;
    *)       PIPER_ASSET="" ;;
esac

if [ -x "$PIPER_BIN" ]; then
    echo "      ✓ Piper binary already installed"
elif [ -z "$PIPER_ASSET" ]; then
    echo "      ⚠ unrecognized architecture '$ARCH' — no prebuilt Piper binary available."
    echo "        Install manually from https://github.com/rhasspy/piper/releases into $PIPER_DIR"
else
    echo "      Downloading Piper ($ARCH)…"
    TMP_TAR="$(mktemp --suffix=.tar.gz)"
    if wget -q -O "$TMP_TAR" "https://github.com/rhasspy/piper/releases/download/${PIPER_RELEASE}/${PIPER_ASSET}"; then
        mkdir -p "$PIPER_DIR"
        tar -xzf "$TMP_TAR" -C "$PIPER_DIR" --strip-components=1
        chmod +x "$PIPER_BIN"
        echo "      ✓ Piper binary installed at $PIPER_BIN"
    else
        echo "      ✗ download failed — check network and retry, or install manually"
    fi
    rm -f "$TMP_TAR"
fi

download_piper_voice() {
    local voice="$1" hf_path="$2"
    local dir="$VOICES_DIR/$voice"
    if [ -f "$dir/$voice.onnx" ]; then
        echo "      ✓ voice '$voice' already present"
        return
    fi
    echo "      Downloading voice '$voice'…"
    mkdir -p "$dir"
    if wget -q -O "$dir/$voice.onnx" "https://huggingface.co/rhasspy/piper-voices/resolve/main/${hf_path}/${voice}.onnx" \
        && wget -q -O "$dir/$voice.onnx.json" "https://huggingface.co/rhasspy/piper-voices/resolve/main/${hf_path}/${voice}.onnx.json"; then
        echo "      ✓ voice '$voice' installed"
    else
        echo "      ✗ voice '$voice' download failed — check network and retry"
        rm -f "$dir/$voice.onnx" "$dir/$voice.onnx.json"
    fi
}
download_piper_voice "en_US-lessac-medium" "en/en_US/lessac/medium"
download_piper_voice "zh_CN-huayan-medium" "zh/zh_CN/huayan/medium"

# ── 4. Speaker verification model (owner-only mode) ──────────────────────────
echo "[4/8] Checking speaker verification model…"
if [ -f "$SPEAKER_MODEL" ]; then
    echo "      ✓ already present"
else
    echo "      Downloading CAM++ speaker-recognition model (~28MB)…"
    mkdir -p "$SPEAKER_MODEL_DIR"
    if wget -q -O "$SPEAKER_MODEL" \
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"; then
        echo "      ✓ installed — enroll your voice later at http://<pi-ip>:19000/voice-enroll"
    else
        echo "      ✗ download failed — owner-only voice verification will stay disabled"
        rm -f "$SPEAKER_MODEL"
    fi
fi

# ── 5. OpenAI API key ─────────────────────────────────────────────────────────
echo "[5/8] Checking OpenAI API key…"
HAS_KEY=0
if [ -f "$OPENCLAW_CONFIG" ] && "$PYTHON" -c "
import json, sys
with open('$OPENCLAW_CONFIG') as f:
    cfg = json.load(f)
key = cfg.get('talk', {}).get('providers', {}).get('openai', {}).get('apiKey', '')
sys.exit(0 if key else 1)
" 2>/dev/null; then
    HAS_KEY=1
fi

if [ "$HAS_KEY" = "1" ]; then
    echo "      ✓ already set at talk.providers.openai.apiKey"
elif [ ! -f "$OPENCLAW_CONFIG" ]; then
    echo "      ⚠ $OPENCLAW_CONFIG not found — is OpenClaw installed and configured yet?"
    echo "        Add talk.providers.openai.apiKey manually once it exists, then re-run this script."
else
    echo "      No key found. It's never printed or logged — input is hidden."
    read -rsp "      Enter an OpenAI API key now (blank to skip and set later): " ENTERED_KEY
    echo ""
    if [ -n "$ENTERED_KEY" ]; then
        "$PYTHON" - "$ENTERED_KEY" <<'PYEOF'
import json, os, sys
key = sys.argv[1]
path = os.path.expanduser("~/.openclaw/openclaw.json")
with open(path) as f:
    cfg = json.load(f)
cfg.setdefault("talk", {}).setdefault("providers", {}).setdefault("openai", {})["apiKey"] = key
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
os.chmod(path, 0o600)
PYEOF
        echo "      ✓ key written to talk.providers.openai.apiKey"
    else
        echo "      Skipped — the service will fail to start until you set:"
        echo "        talk.providers.openai.apiKey  in  $OPENCLAW_CONFIG"
    fi
fi

# ── 6. Audio devices ──────────────────────────────────────────────────────────
echo "[6/8] Detecting audio devices…"
echo ""
"$PYTHON" "$DAEMON" --list-devices 2>/dev/null || true
echo ""
echo "      Mic/speaker are auto-selected via PipeWire's own default source/sink"
echo "      (see step 7) — no device index needed here.  Use the dashboard's"
echo "      'Use' buttons, or 'pactl set-default-source/-sink <name>', to pick"
echo "      a specific device; the daemon follows PipeWire's default at runtime."

# ── 7. systemd user service ───────────────────────────────────────────────────
echo "[7/8] Writing systemd service…"
mkdir -p "$SERVICE_DIR"

# "pipewire" / "default" are PipeWire's own ALSA-compat pseudo-devices (from the
# pipewire-alsa package installed in step 1) — they resample to whatever rate is
# requested and follow PipeWire's default source/sink, so they survive USB
# hotplug unlike a raw numeric device index (which shifts every time a USB
# audio device is plugged or unplugged). Edit only if you have a specific
# reason to pin a raw ALSA device instead, then re-run this script.
INPUT_DEVICE="pipewire"
ALSA_OUTPUT="default"

build_exec_start() {
    local cmd="$PYTHON $DAEMON"
    [ "$INPUT_DEVICE" != "none" ] && cmd="$cmd --input-device $INPUT_DEVICE"
    [ "$ALSA_OUTPUT"  != "none" ] && cmd="$cmd --alsa-output $ALSA_OUTPUT"
    echo "$cmd"
}

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

# ── 8. Enable + start ─────────────────────────────────────────────────────────
echo "[8/8] Enabling service…"
loginctl enable-linger "$USER" 2>/dev/null || true
echo "      ✓ linger enabled for $USER"

systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME"
systemctl --user restart "$SERVICE_NAME"
echo "      ✓ service enabled and (re)started"

echo ""
echo "=== Done ==="
echo ""
echo "  Status:  systemctl --user status $SERVICE_NAME"
echo "  Logs:    journalctl --user -u $SERVICE_NAME -f"
echo "  Stop:    systemctl --user stop $SERVICE_NAME"
echo "  Dashboard: http://<pi-ip>:19000/dashboard"
echo ""
if [ "$HAS_KEY" != "1" ] && [ -z "${ENTERED_KEY:-}" ]; then
    echo "  ⚠ Remember: no OpenAI API key set yet — the service will not stay running"
    echo "    until talk.providers.openai.apiKey is set in $OPENCLAW_CONFIG"
    echo ""
fi
echo "To pin a specific mic/speaker instead of PipeWire's default:"
echo "  1. Edit INPUT_DEVICE / ALSA_OUTPUT near the bottom of this script"
echo "  2. Re-run: bash $(basename "$0")"
