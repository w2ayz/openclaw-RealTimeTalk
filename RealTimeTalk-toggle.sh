#!/usr/bin/env bash
# RealTimeTalk-toggle.sh — manage the openclaw-realtimetalk service
#
# Usage: RealTimeTalk-toggle.sh {start|stop|restart|status|log|devices}

SERVICE="openclaw-realtimetalk"
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CMD="${1:-status}"

case "$CMD" in
  start)
    systemctl --user start "$SERVICE"
    echo "Started."
    ;;
  stop)
    systemctl --user stop "$SERVICE"
    echo "Stopped."
    ;;
  restart)
    systemctl --user restart "$SERVICE"
    echo "Restarted."
    ;;
  status)
    systemctl --user status "$SERVICE"
    ;;
  log)
    journalctl --user -u "$SERVICE" -f
    ;;
  devices)
    python3 "$SKILL_DIR/RealTimeTalk-daemon.py" --list-devices
    ;;
  *)
    echo "Usage: $(basename "$0") {start|stop|restart|status|log|devices}"
    exit 1
    ;;
esac
