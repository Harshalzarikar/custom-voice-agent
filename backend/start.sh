#!/usr/bin/env bash
# One container: LiveKit worker (restarted if it crashes) and a single API worker.
# A single API worker matters because agent records are a JSON file. Extra
# Gunicorn processes would each serve a stale copy.
set -uo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-5000}"

shutdown() {
  trap - EXIT INT TERM
  kill -TERM 0 2>/dev/null || true
  wait 2>/dev/null || true
}
trap shutdown EXIT INT TERM

(
  delay=2
  while true; do
    python agent_worker.py start || true
    echo "agent worker exited; restarting in ${delay}s" >&2
    sleep "${delay}"
    if (( delay < 30 )); then
      delay=$((delay + 2))
    fi
  done
) &

gunicorn main:app \
  -w 1 \
  -k uvicorn.workers.UvicornWorker \
  --bind "0.0.0.0:${PORT}" \
  --timeout 120 \
  --graceful-timeout 30 \
  --keep-alive 5 \
  --access-logfile - \
  --error-logfile - &

wait -n || true
shutdown
