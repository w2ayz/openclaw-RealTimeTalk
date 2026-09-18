#!/usr/bin/env bash
# RealTimeTalk-configure.sh — re-runnable setup for STT keys/engine, TTS
# keys/engine order, and STT custom vocabulary. Safe to run anytime after
# the initial install (RealTimeTalk-install-pi.sh) — it never touches apt
# packages, the Python venv, Piper voices, or the systemd user service unit.
# Use it to add a key you skipped earlier, change the TTS engine order, or
# add more vocabulary terms.
#
# Usage:
#   bash RealTimeTalk-configure.sh

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$HOME/.local/realtimetalk-venv"
PYTHON="$VENV/bin/python"
OPENCLAW_CONFIG="$HOME/.openclaw/openclaw.json"
STT_CFG="$HOME/.openclaw/workspace/rtt_stt_config.json"
TTS_CFG="$HOME/.openclaw/workspace/rtt_tts_config.json"

# shellcheck source=RealTimeTalk-config-lib.sh
source "$SKILL_DIR/RealTimeTalk-config-lib.sh"

if [ ! -x "$PYTHON" ]; then
    echo "  ✗ venv not found at $PYTHON — run RealTimeTalk-install-pi.sh first."
    exit 1
fi
if [ ! -f "$OPENCLAW_CONFIG" ]; then
    echo "  ✗ $OPENCLAW_CONFIG not found — run RealTimeTalk-install-pi.sh first."
    exit 1
fi

echo "=== RealTimeTalk configure ==="
echo ""
echo "  Every step below can be skipped — press Enter to keep what's already"
echo "  there. This whole script is safe to re-run anytime:"
echo "    bash \"$SKILL_DIR/RealTimeTalk-configure.sh\""
echo "  Run it again later to add a key you skipped now, change the TTS engine"
echo "  order, or add more STT vocabulary."
echo ""

run_stt_setup
run_tts_setup
run_vocabulary_setup

echo ""
echo "=== Configure complete ==="
echo ""
read -rp "Restart the RealTimeTalk service now to apply changes? [y/N]: " DO_RESTART
case "$DO_RESTART" in
    [Yy]*) bash "$SKILL_DIR/RealTimeTalk-toggle.sh" restart ;;
    *)
        echo "  → Not restarted. Changes take effect on next restart:"
        echo "    bash \"$SKILL_DIR/RealTimeTalk-toggle.sh\" restart"
        ;;
esac
echo ""
echo "Re-run this anytime: bash \"$SKILL_DIR/RealTimeTalk-configure.sh\""
