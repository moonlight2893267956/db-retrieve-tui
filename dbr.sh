#!/bin/bash
# dbr 启动脚本（Python 版）
#
# 自动探测可用的 Python 2.7 运行时并启动 dbr.py。
# - 优先系统自带 /usr/bin/python（目标机器的 2.7.5）
# - 支持 DBR_PYTHON 显式覆盖
# - 支持通过软链 `dbr` 调用（不受当前目录影响）
#
# 用法：
#   ./dbr.sh                        # 交互终端（默认配置）
#   ./dbr.sh --config=<配置.py>      # 指定配置
#   ./dbr.sh --from-alias           # 复用 shell alias 的 mysql 连接
#   ./dbr.sh --help
#   DBR_PYTHON=/path/to/python ./dbr.sh

SCRIPT_DIR=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)

# 解析软链真实路径（兼容 mac 与 linux）
resolve() {
  local f="$1"
  while [ -L "$f" ]; do
    local t
    t=$(readlink "$f")
    case "$t" in
      /*) f="$t" ;;
      *)  f="$(dirname "$f")/$t" ;;
    esac
  done
  echo "$f"
}
REAL_SCRIPT=$(resolve "$0")
SCRIPT_DIR=$(cd "$(dirname "$REAL_SCRIPT")" && pwd)

# 候选 Python 运行时（按优先级）
PY_CANDIDATES=(
  "$DBR_PYTHON"
  "/usr/bin/python"
  "/usr/bin/python2"
  "/usr/bin/python2.7"
  "$(command -v python2.7 2>/dev/null)"
  "$(command -v python2 2>/dev/null)"
  "$(command -v python 2>/dev/null)"
)

pick_python() {
  local c
  for c in "${PY_CANDIDATES[@]}"; do
    [ -n "$c" ] && [ -x "$c" ] || continue
    # 校验 2.7 且可 import 标准库
    if "$c" -c 'import sys; sys.exit(0 if sys.version_info[:2]==(2,7) else 1)' >/dev/null 2>&1; then
      echo "$c"
      return 0
    fi
  done
  return 1
}

PY_BIN=$(pick_python)
if [ -z "$PY_BIN" ]; then
  echo "[错误] 未找到可用的 Python 2.7 运行时。" >&2
  echo "" >&2
  echo "  已尝试的候选：" >&2
  for c in "${PY_CANDIDATES[@]}"; do
    [ -n "$c" ] && echo "    - $c" >&2
  done
  echo "" >&2
  echo "  排查命令：" >&2
  echo "    /usr/bin/python -V" >&2
  echo "    which python2.7 python2 python" >&2
  echo "  显式指定：export DBR_PYTHON=/路径/python 或 DBR_PYTHON=/路径/python ./dbr.sh" >&2
  exit 1
fi

exec "$PY_BIN" "$SCRIPT_DIR/dbr.py" "$@"
