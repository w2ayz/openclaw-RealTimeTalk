#!/usr/bin/env bash
# RealTimeTalk-toggle.sh — manage the openclaw-realtimetalk service
#
# Usage: RealTimeTalk-toggle.sh {start|stop|restart|disable|enable|status|log|devices}

SERVICE="openclaw-realtimetalk"
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CMD="${1:-status}"
PORT=19000   # daemon's default HTTP dashboard port (DEFAULT_HTTP_PORT)

case "$CMD" in
  start)
    systemctl --user start "$SERVICE"
    echo "Started."
    ;;
  stop)
    systemctl --user stop "$SERVICE"
    echo "Stopped (still starts on next login/boot — use 'disable' to prevent that)."
    ;;
  restart)
    systemctl --user restart "$SERVICE"
    echo "Restarted."
    ;;
  disable)
    # Stop now AND prevent autostart on the next login/boot — no mic
    # capture, no OpenAI Realtime connection, dashboard down — without
    # uninstalling. This is the mic kill-switch. systemd stops the whole
    # cgroup, so there is nothing to reap.
    systemctl --user disable --now "$SERVICE"
    if systemctl --user is-active --quiet "$SERVICE"; then
      echo "  WARN: $SERVICE still reports active — check: systemctl --user status $SERVICE"
      exit 1
    fi
    echo "  OK: $SERVICE stopped and disabled (persists across reboots; mic released)."
    echo "      Re-enable with: $(basename "$0") enable"
    ;;
  enable)
    # Undo 'disable': re-enable autostart and start it now.
    systemctl --user enable --now "$SERVICE"
    for _ in $(seq 1 20); do
      if curl -fsS -m 2 "http://127.0.0.1:$PORT/status" 2>/dev/null; then
        echo
        echo "  OK: RTT is up."
        exit 0
      fi
      sleep 1
    done
    echo "  WARN: no /status response after 20s — check: $(basename "$0") log"
    exit 1
    ;;
  status)
    systemctl --user status "$SERVICE"
    echo "enabled state: $(systemctl --user is-enabled "$SERVICE" 2>/dev/null || echo unknown)"
    ;;
  log)
    journalctl --user -u "$SERVICE" -f
    ;;
  devices)
    python3 "$SKILL_DIR/RealTimeTalk-daemon.py" --list-devices
    ;;
  *)
    echo "Usage: $(basename "$0") {start|stop|restart|disable|enable|status|log|devices}"
    echo
    echo "  start    systemctl --user start"
    echo "  stop     systemctl --user stop   (comes back at next login/boot)"
    echo "  restart  systemctl --user restart"
    echo "  disable  stop + disable autostart (mic kill-switch; persists across reboots)"
    echo "  enable   undo disable, wait until the dashboard answers"
    echo "  status   systemctl --user status (+ enabled/disabled state)"
    echo "  log      journalctl --user -u ... -f"
    echo "  devices  list audio devices the daemon can see"
    exit 1
    ;;
esac
