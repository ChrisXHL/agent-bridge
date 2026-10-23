#!/bin/sh
set -eu
umask 077
cd "$(dirname "$0")"
PATH="$PWD/.tools/node_modules/.bin:$PWD/.venv/bin:$PATH"
export PATH
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi
if [ ! -x .venv/bin/python ]; then
  echo '先创建 .venv 并安装 requirements.txt' >&2
  exit 1
fi
exec .venv/bin/python service.py --db "${BRIDGE_DB:-data/bridge.sqlite3}" --projects "${BRIDGE_PROJECTS:-projects.json}" --port "${BRIDGE_PORT:-8797}"
