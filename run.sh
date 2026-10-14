#!/usr/bin/env bash
# One-click runner for the two-sided maker replication test.
#
#   ./run.sh                 paper mode in the foreground (no keys, no orders)
#   ./run.sh start           paper mode in the background
#   ./run.sh start --execute live orders in the background (needs .env, passes geoblock)
#   ./run.sh status | stop | logs | summary | report | latency | test
#
# Config: copy .env.example to .env and fill it in; see README.md.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
RUNTIME="$DIR/runtime"
VENV="$DIR/.venv"
PY="$VENV/bin/python"
PIDFILE="$RUNTIME/bot.pid"
LOG="$RUNTIME/bot.log"
JOURNAL="$RUNTIME/journal.jsonl"
REF_WALLET_DEFAULT="0x21f4c1a1be34a39a7bc20985df4b7e5a39fb691d"

mkdir -p "$RUNTIME"

load_env() {
  if [[ -f "$DIR/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$DIR/.env"
    set +a
  fi
}

ensure_venv() {
  if [[ -x "$PY" ]] && "$PY" -c "import polymarket, websockets" 2>/dev/null; then
    return
  fi
  if ! command -v uv >/dev/null 2>&1; then
    echo "uv not found. Install it: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
  fi
  echo "Setting up Python 3.12 venv in $VENV ..."
  uv venv -q -p 3.12 "$VENV"
  uv pip install -q -p "$PY" -r "$DIR/requirements.txt"
}

is_running() {
  [[ -f "$PIDFILE" ]] && ps -p "$(cat "$PIDFILE")" >/dev/null 2>&1
}

check_live_env() {
  local missing=()
  for v in PM_PRIVATE_KEY PM_RELAYER_API_KEY PM_RELAYER_API_KEY_ADDRESS; do
    [[ -n "${!v:-}" ]] || missing+=("$v")
  done
  if (( ${#missing[@]} )); then
    echo "missing in .env: ${missing[*]}" >&2
    exit 1
  fi
}

run_args() {
  local args=(run --journal "$JOURNAL")
  [[ "${1:-}" == "--execute" ]] && args+=(--execute)
  [[ -n "${MR_RUN_MINUTES:-}" ]] && args+=(--minutes "$MR_RUN_MINUTES")
  echo "${args[@]}"
}

cmd="${1:-fg}"
shift || true
load_env

case "$cmd" in
  fg)
    ensure_venv
    [[ "${1:-}" == "--execute" ]] && check_live_env
    cd "$DIR" && exec "$PY" bot.py $(run_args "${1:-}")
    ;;
  start)
    ensure_venv
    if is_running; then echo "already running (pid $(cat "$PIDFILE"))"; exit 0; fi
    [[ "${1:-}" == "--execute" ]] && check_live_env
    cd "$DIR"
    nohup "$PY" bot.py $(run_args "${1:-}") >>"$LOG" 2>&1 &
    echo $! >"$PIDFILE"
    sleep 3
    if is_running; then
      echo "started pid $(cat "$PIDFILE") (${1:-paper}); logs: ./run.sh logs"
    else
      echo "bot exited during startup:" >&2
      tail -20 "$LOG" >&2
      exit 1
    fi
    ;;
  stop)
    if is_running; then
      kill -TERM "$(cat "$PIDFILE")"
      echo "stop signal sent; open orders are cancelled, pending settlements finish before exit"
    else
      echo "not running"
    fi
    ;;
  status)
    if is_running; then echo "running pid $(cat "$PIDFILE")"; else echo "not running"; fi
    [[ -f "$JOURNAL" ]] && tail -3 "$JOURNAL"
    ;;
  logs)
    tail -f "$LOG"
    ;;
  summary)
    ensure_venv
    cd "$DIR" && "$PY" bot.py summary --journal "$JOURNAL"
    ;;
  report)
    # Per-fill edge of our wallet vs the reference wallet over the bot's run window.
    ensure_venv
    cd "$DIR"
    start_ts="$("$PY" bot.py summary --journal "$JOURNAL" | awk '/^start_ts/ {print $2}')"
    wallet="$("$PY" - "$JOURNAL" <<'EOF'
import json, sys
w = None
for line in open(sys.argv[1]):
    r = json.loads(line)
    if r["event"] == "start" and r.get("mode") == "live":
        w = r["wallet"]
print(w or "")
EOF
)"
    if [[ -z "$wallet" ]]; then
      echo "no live run in the journal; for paper runs use ./run.sh summary" >&2
      exit 1
    fi
    "$PY" fill_edge_report.py --wallet "$wallet" --wallet "${MR_REF_WALLET:-$REF_WALLET_DEFAULT}" \
      --start "$start_ts" --kinds "${MR_KINDS:-5m}" \
      --taker-rebate-pct "${MR_TAKER_REBATE_PCT:-0.18}" --json-out "$RUNTIME/edge_report.json" "$@"
    ;;
  latency)
    # Network check of this machine against the CLOB; no keys needed.
    ensure_venv
    cd "$DIR" && "$PY" latency.py
    ;;
  test)
    ensure_venv
    cd "$DIR" && "$PY" -m unittest discover -s tests
    ;;
  *)
    sed -n '2,10p' "$0"
    exit 1
    ;;
esac
