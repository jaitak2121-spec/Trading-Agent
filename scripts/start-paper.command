#!/bin/zsh
set -eu
unsetopt BG_NICE
project_dir=${0:A:h:h}
cd -- "$project_dir"
if [[ ! -x .venv/bin/python ]]; then
  print 'Create .venv and install the project using docs/OPERATING_GUIDE.md first.'
  exit 1
fi
if [[ ${1:-} == --help ]]; then
  exec .venv/bin/python -m trading.app --help
fi
for option in "$@"; do
  case "$option" in
    --state-db|--state-db=*|--learning-reports|--learning-reports=*|--coinswitch-symbol|--coinswitch-symbol=*|--coinswitch-exchange|--coinswitch-exchange=*|--dashboard|--port|--port=*|--host|--host=*)
      print 'Use the direct CLI for a custom data profile; this launcher keeps collection and learning on the same BTC/USDT database.'
      exit 2 ;;
  esac
done
mkdir -p artifacts/real-paper-learning
.venv/bin/python - <<'PY'
import json
from pathlib import Path
p=Path('artifacts/real-paper-learning/job-v032.local.json')
if not p.exists():
    c=json.loads(Path('config/experience-job.example.json').read_text())
    c.update(enabled=True,interval_seconds=3600,state_db='artifacts/real-paper-btc-usdt-v032.sqlite3',
             registry_db='artifacts/real-paper-model-registry.sqlite3',output_directory='artifacts/real-paper-learning')
    with p.open('x') as handle:json.dump(c,handle,indent=2)
PY
paper_pid=''
learning_pid=''
cleanup() {
  [[ -z $learning_pid ]] || kill -TERM "$learning_pid" 2>/dev/null || true
  [[ -z $paper_pid ]] || kill -TERM "$paper_pid" 2>/dev/null || true
  [[ -z $learning_pid ]] || wait "$learning_pid" 2>/dev/null || true
  [[ -z $paper_pid ]] || wait "$paper_pid" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT TERM
.venv/bin/python -m trading.app --coinswitch-symbol BTC/USDT --coinswitch-exchange c2c1 \
  --config config/paper-usdt.example.toml --cash 10000 --fee-bps 10 --slippage-bps 2 \
  --state-db artifacts/real-paper-btc-usdt-v032.sqlite3 --learning-reports artifacts/real-paper-learning \
  --paper-controls --port 8766 "$@" &
paper_pid=$!
.venv/bin/python -c 'import sys; from trading.adapters.http.dashboard import wait_for_paper_readiness; wait_for_paper_readiness(int(sys.argv[1]))' "$paper_pid"
.venv/bin/python -m trading.app.research_jobs artifacts/real-paper-learning/job-v032.local.json \
  >> artifacts/real-paper-learning/scheduler.log 2>&1 &
learning_pid=$!
wait "$paper_pid"
