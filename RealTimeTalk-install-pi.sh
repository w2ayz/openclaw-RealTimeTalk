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
# mpg123          - decodes the MP3 the Edge TTS skill emits into WAV for the
#                   ElevenLabs → Edge → OpenAI → Piper fallback chain (tiny package)
REQUIRED_APT_PKGS=(libportaudio2 pulseaudio-utils pipewire-alsa espeak-ng fonts-noto-color-emoji sox multimon-ng mpg123)
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

# ── 3b. Edge TTS skill (network TTS fallback for Chinese/mixed — optional) ────
# Sits between ElevenLabs and OpenAI TTS in the chain: free, no API key, native
# zh-CN / en-US neural voices. Needs the published edge-tts skill (official
# location ~/.openclaw/workspace/skills/edge-tts/) plus Node.js. Absent skill or
# Node → warn and continue; ElevenLabs → OpenAI TTS → Piper still cover every reply.
echo "      Checking Edge TTS skill (optional network fallback)…"
EDGE_TTS_OFFICIAL="$HOME/.openclaw/workspace/skills/edge-tts/scripts/tts-converter.js"
EDGE_TTS_SCRIPT=""
for cand in \
    "$SKILL_DIR/../edge-tts/scripts/tts-converter.js" \
    "${OPENCLAW_WORKSPACE:-}/skills/edge-tts/scripts/tts-converter.js" \
    "$EDGE_TTS_OFFICIAL"; do
    if [ -n "$cand" ] && [ -f "$cand" ]; then
        EDGE_TTS_SCRIPT="$(cd "$(dirname "$cand")" && pwd)/$(basename "$cand")"
        break
    fi
done

if [ -z "$EDGE_TTS_SCRIPT" ]; then
    echo "      ⚠ edge-tts skill not found — install it to enable the Edge TTS tier:"
    echo "          clawhub install edge-tts"
    echo "        Skipping. ElevenLabs → OpenAI TTS → Piper still cover all replies."
    EDGE_TTS_SCRIPT="$EDGE_TTS_OFFICIAL"   # daemon re-checks this path at runtime
else
    if ! command -v node >/dev/null 2>&1; then
        echo "      Node.js not found — installing (needed to run the edge-tts skill)…"
        sudo apt-get install -y -q nodejs npm || \
            echo "      ⚠ nodejs/npm install failed — Edge TTS stays disabled until Node is available."
    fi
    if command -v node >/dev/null 2>&1; then
        EDGE_TTS_DIR="$(cd "$(dirname "$EDGE_TTS_SCRIPT")" && pwd)"
        if [ ! -d "$EDGE_TTS_DIR/node_modules" ]; then
            echo "      Installing Edge TTS node deps (npm install in $EDGE_TTS_DIR)…"
            ( cd "$EDGE_TTS_DIR" && npm install --omit=dev --silent ) || \
                echo "      ⚠ npm install failed — Edge TTS falls back to OpenAI/Piper at runtime."
        fi
        if node "$EDGE_TTS_SCRIPT" --help >/dev/null 2>&1; then
            echo "      ✓ Edge TTS skill ready ($EDGE_TTS_SCRIPT)"
        else
            echo "      ⚠ Edge TTS script present but not runnable — check node + node_modules."
        fi
    fi
fi

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

# ── 5. STT keys/engine, TTS keys/engine order, STT vocabulary ────────────────
# Keys live in openclaw.json (talk.providers.<name>.apiKey — including
# elevenlabs as of v3.23.0, migrated off the old flat secrets file). Engine/
# order choices live in the daemon's own config files
# (~/.openclaw/workspace/rtt_stt_config.json, rtt_tts_config.json) — NOT in
# openclaw.json: OpenClaw's TalkSchema has no `stt` key, so a talk.stt block
# there gets stripped by gateway config rewrites and fails
# `openclaw config validate`. The daemon still reads a legacy openclaw.json
# talk.stt block as a fallback source, but this installer never writes one.
#
# All three interview steps live in RealTimeTalk-config-lib.sh so
# RTT-Config.sh can re-run them later without repeating the rest
# of this installer. run_stt_setup now includes a Skip option (TTS-only, no
# STT key at all) — the daemon resolves that to STT_ENGINE_NONE and runs
# text-only instead of crash-looping under systemd waiting for a key.
echo "[5/8] STT provider keys, TTS engine order…"

STT_CFG="$HOME/.openclaw/workspace/rtt_stt_config.json"
TTS_CFG="$HOME/.openclaw/workspace/rtt_tts_config.json"

# shellcheck source=RealTimeTalk-config-lib.sh
source "$SKILL_DIR/RealTimeTalk-config-lib.sh"

run_stt_setup
run_tts_setup
echo ""
run_vocabulary_setup

# ── 6. Audio devices ──────────────────────────────────────────────────────────
echo "[6/8] Detecting audio devices…"
echo ""
"$PYTHON" "$DAEMON" --list-devices 2>/dev/null || true
echo ""
echo "      Mic/speaker are auto-selected via PipeWire's own default source/sink"
echo "      (see step 7) — no device index needed here.  Use the dashboard's"
echo "      'Use' buttons, or 'pactl set-default-source/-sink <name>', to pick"
echo "      a specific device; the daemon follows PipeWire's default at runtime."

# ── 7. Agent identity + systemd user service ──────────────────────────────────
echo "[7/8] Configuring agent identity…"

# Preserve the existing agent name / wake phrase across re-runs (e.g. after
# `git pull`) instead of silently reprompting with the new code default —
# a re-run must never rename a live deployment out from under it.
EXISTING_AGENT_NAME=""
EXISTING_WAKE_PHRASE=""
if [ -f "$SERVICE_FILE" ]; then
    EXISTING_AGENT_NAME="$(grep -oP -- '--agent-name \K\S+' "$SERVICE_FILE" 2>/dev/null || true)"
    EXISTING_WAKE_PHRASE="$(grep -oP -- '--wake-phrase "\K[^"]+' "$SERVICE_FILE" 2>/dev/null || true)"
fi

if [ -n "$EXISTING_AGENT_NAME" ]; then
    DEFAULT_AGENT_NAME="$EXISTING_AGENT_NAME"
elif [ -f "$SERVICE_FILE" ]; then
    # Pre-existing install from before --agent-name existed — it was always "Five".
    DEFAULT_AGENT_NAME="Five"
else
    DEFAULT_AGENT_NAME="Zeebot"
fi

read -r -p "      Agent name  [Enter for '$DEFAULT_AGENT_NAME']: " AGENT_NAME_ARG
AGENT_NAME_ARG="${AGENT_NAME_ARG:-$DEFAULT_AGENT_NAME}"

WAKE_PHRASE_PROMPT_DEFAULT="${EXISTING_WAKE_PHRASE:-${AGENT_NAME_ARG,,} wake up}"
read -r -p "      Wake phrase [Enter for '$WAKE_PHRASE_PROMPT_DEFAULT']: " WAKE_PHRASE_ARG
WAKE_PHRASE_ARG="${WAKE_PHRASE_ARG:-$WAKE_PHRASE_PROMPT_DEFAULT}"

echo "      ✓ agent name: $AGENT_NAME_ARG   wake phrase: $WAKE_PHRASE_ARG"

echo "      Writing systemd service…"
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
    cmd="$cmd --agent-name \"$AGENT_NAME_ARG\" --wake-phrase \"$WAKE_PHRASE_ARG\""
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
Environment=RTT_EDGE_TTS_SCRIPT=$EDGE_TTS_SCRIPT

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
if [ "$HAS_KEY" != "1" ]; then
    echo "  ⚠ Reminder: no STT provider key set yet (openai or gemini) — the service"
    echo "    will not stay running until one is set in $OPENCLAW_CONFIG"
    echo ""
fi
echo "To pin a specific mic/speaker instead of PipeWire's default:"
echo "  1. Edit INPUT_DEVICE / ALSA_OUTPUT near the bottom of this script"
echo "  2. Re-run: bash $(basename "$0")"
echo ""
echo "To change agent name or wake phrase later: bash $(basename "$0") — re-running"
echo "prompts again (Enter keeps the current name/phrase, shown as the default)."
if has_openai_key; then
    echo ""
    echo "  Note: OpenAI's realtime STT (gpt-live-transcribe) has no server-side"
    echo "  voice detection — this daemon's own noise gate (MIC_GATE_PEAK /"
    echo "  AGC_MIC_GATE, or --mic-gate with --input-source) is the only signal"
    echo "  deciding when you've stopped talking. If OpenAI transcripts never"
    echo "  finalize (mic seems to 'hang open'), run:"
    echo "    $PYTHON $DAEMON --calibrate --input-device \$INPUT_DEVICE"
    echo "  and pass the recommended value as --mic-gate (with --input-source),"
    echo "  or lower AGC_MIC_GATE near the top of RealTimeTalk-daemon.py if the"
    echo "  AGC source is active."
fi
