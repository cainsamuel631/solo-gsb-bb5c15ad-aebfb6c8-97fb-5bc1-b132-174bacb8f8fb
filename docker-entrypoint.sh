#!/bin/sh
# 启动即初始化 SQLite（幂等建表），随后 exec 主进程
set -e

DB_PATH="${SUB_DB_PATH:-/data/subtitles.db}"
DB_DIR="$(dirname "$DB_PATH")"
mkdir -p "$DB_DIR"

SUB_DB_PATH="$DB_PATH" python - <<'PY'
import os
from app.database import DB_PATH, init_db
init_db()
print("database initialized at", DB_PATH)
PY

exec "$@"
