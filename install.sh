#!/bin/bash
# =============================================================================
# dbr 交互式部署脚本（Python 版）
#
# 作用：在测试机 / 运维机 / 线上环境一次性完成可用化配置：
#   1) 探测可用、且为 Python 2.7 的运行时；
#   2) 交互式选择运行时（可手输 python 路径）；
#   3) 校验 mysql 客户端（/bin/mysql）是否可用；
#   4) 生成软链 ~/bin/dbr（默认），之后任意目录可直接 `dbr`；
#   5) 确保软链所在目录在 PATH（不在则自动写入 shell 启动文件）；
#   6) 校验软链可用（dbr --help）。
#
# 用法：
#   ./install.sh                        # 交互式引导
#   ./install.sh --help
#   DBR_PYTHON=/路径/python ./install.sh # 非交互：直接用指定 python
# =============================================================================

set -u

# 兼容 $HOME 未设置的环境（如部分非交互/受限 shell）：用 passwd 记录兜底
if [ -z "${HOME:-}" ]; then
  HOME="$(getent passwd "$(id -un 2>/dev/null)" 2>/dev/null | cut -d: -f6)"
  [ -n "${HOME:-}" ] || HOME="$(cd ~ 2>/dev/null && pwd)"
  export HOME
fi
if [ -z "${HOME:-}" ]; then
  echo "[错误] 无法确定用户主目录（\$HOME 未设置）。请先 export HOME=/你的主目录" >&2
  exit 1
fi

SCRIPT_DIR=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)

if [ -t 1 ]; then
  C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_CYAN=$'\033[36m'; C_OFF=$'\033[0m'
else
  C_GREEN=""; C_YELLOW=""; C_RED=""; C_CYAN=""; C_OFF=""
fi
ok()   { echo "${C_GREEN}✔${C_OFF} $*"; }
info() { echo "${C_CYAN}·${C_OFF} $*"; }
warn() { echo "${C_YELLOW}⚠${C_OFF} $*"; }
fail() { echo "${C_RED}✘${C_OFF} $*"; }

usage() {
  cat <<'EOF'
dbr 交互式部署脚本（Python 版）

用法:
  ./install.sh                          # 交互式引导（推荐）
  ./install.sh --help                   # 显示本帮助
  DBR_PYTHON=/路径/python ./install.sh   # 非交互：直接用指定 python

步骤:
  1. 探测可用 Python 2.7 运行时
  2. 选择运行时（可手输路径）
  3. 校验 mysql 客户端
  4. 生成软链 ~/bin/dbr（可改）
  5. 确保软链目录在 PATH（不在则写入 shell 启动文件）
  6. 校验 dbr --help

完成后即可在任意目录直接输入 dbr（作为系统指令）。
若第 5 步写入了 PATH，需重开终端或 source 相应启动文件后生效。
EOF
}

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  usage; exit 0
fi

# 候选 Python（按优先级）
PY_CANDIDATES=(
  "${DBR_PYTHON:-}"
  "/usr/bin/python"
  "/usr/bin/python2"
  "/usr/bin/python2.7"
  "$(command -v python2.7 2>/dev/null)"
  "$(command -v python2 2>/dev/null)"
  "$(command -v python 2>/dev/null)"
)

probe_python() {
  local c="$1"
  [ -n "$c" ] && [ -x "$c" ] || return 1
  "$c" -c 'import sys; sys.exit(0 if sys.version_info[:2]==(2,7) else 1)' >/dev/null 2>&1
}

echo
echo "=============================================="
echo " dbr · 交互式部署（Python 版）"
echo "=============================================="
echo "  项目目录: ${SCRIPT_DIR}"
echo

# ---- 收集可用运行时 ----
declare -a AVAILABLE=()
for c in "${PY_CANDIDATES[@]}"; do
  [ -n "$c" ] || continue
  if probe_python "$c"; then
    # 去重
    dup=0
    for e in "${AVAILABLE[@]:-}"; do [ "$e" = "$c" ] && dup=1; done
    [ "$dup" = "0" ] && AVAILABLE+=("$c")
  fi
done

if [ "${#AVAILABLE[@]}" -gt 0 ]; then
  echo "  ── 运行时 ──────────────────────────────────────────────"
  echo "  以下 Python 2.7 运行时可用："
  idx=1
  for e in "${AVAILABLE[@]}"; do
    echo "    ${idx}) $e  ($("$e" -V 2>&1))"
    idx=$((idx+1))
  done
  echo
fi

# ---- 选择运行时 ----
PY_BIN=""
if [ -n "${DBR_PYTHON:-}" ] && probe_python "$DBR_PYTHON"; then
  PY_BIN="$DBR_PYTHON"
  ok "已显式指定 DBR_PYTHON=${PY_BIN}"
elif [ "${#AVAILABLE[@]}" -gt 0 ]; then
  while :; do
    read -rp "  请选择运行时 (1-${#AVAILABLE[@]}/m 手输路径/q 退出) [1]: " sel
    sel="${sel:-1}"
    case "$sel" in
      q|n) echo "  已取消。"; exit 1 ;;
      m)
        read -rp "  输入 python 完整路径: " pbin
        pbin=${pbin/#\~/$HOME}
        if probe_python "$pbin"; then PY_BIN="$pbin"; break
        else fail "该 python 非 2.7 或不可用，请重试。"; fi
        ;;
      *)
        if [ "$sel" -ge 1 ] 2>/dev/null && [ "$sel" -le "${#AVAILABLE[@]}" ] 2>/dev/null; then
          PY_BIN="${AVAILABLE[$((sel-1))]}"; break
        else warn "  无效选择：$sel"; fi
        ;;
    esac
  done
  ok "已选择运行时: ${PY_BIN}"
else
  fail "未探测到可用的 Python 2.7 运行时。"
  echo "  请安装或指定：DBR_PYTHON=/路径/python ./install.sh" >&2
  exit 1
fi

# ---- 校验 mysql 客户端 ----
echo
if [ -x /bin/mysql ] || [ -x /usr/bin/mysql ] || command -v mysql >/dev/null 2>&1; then
  MYSQL_BIN=$(command -v mysql 2>/dev/null || echo /bin/mysql)
  ok "mysql 客户端: ${MYSQL_BIN}"
else
  warn "未找到 mysql 客户端（/bin/mysql）。dbr 依赖它连接数据库，请确认其存在于运行环境。"
fi

# ---- 生成软链（作为系统指令 dbr） ------------------------------------
echo
echo "  ── 生成软链 dbr ──────────────────────────────────────────"
DEFAULT_LINK="${HOME}/bin/dbr"
read -rp "  软链位置 [${DEFAULT_LINK}]: " LINK
[ -z "${LINK:-}" ] && LINK="${DEFAULT_LINK}"
case "$LINK" in
  '~'/*) LINK="${HOME}/${LINK#~/}" ;;
  '~')   LINK="${HOME}" ;;
esac

mkdir -p "$(dirname "$LINK")"
ln -sf "${SCRIPT_DIR}/dbr.sh" "$LINK"
ok "已生成软链: ${LINK} -> ${SCRIPT_DIR}/dbr.sh"

# ---- 值清单文件目录（批量 IN 查询的默认数据目录） ----
mkdir -p "${SCRIPT_DIR}/data"
ok "值清单文件目录: ${SCRIPT_DIR}/data（把 CSV/文本放进去，dbr 会列成菜单供选择）"

# ---- 确保软链所在目录在 PATH（否则 dbr 无法作为系统指令直接调用） ----
LINK_DIR="$(dirname "$LINK")"
in_path_now() {
  case ":${PATH}:" in *":${LINK_DIR}:"*) return 0 ;; *) return 1 ;; esac
}
in_rc_path() {
  # 检查 rc 文件里是否已有把 LINK_DIR 加入 PATH 的持久配置
  local f
  for f in "$HOME/.bash_profile" "$HOME/.bashrc" "$HOME/.profile"; do
    [ -f "$f" ] || continue
    grep -qE "PATH=.*${LINK_DIR//\//\\/}" "$f" 2>/dev/null && return 0
  done
  return 1
}
path_added=""
if in_path_now; then
  ok "${LINK_DIR} 已在当前 PATH 中，可直接输入 dbr 使用。"
elif in_rc_path; then
  info "${LINK_DIR} 已配置在 shell 启动文件中（重开终端即生效）。"
else
  # 选择要写入的启动文件：优先 .bash_profile，其次 .bashrc
  RC="$HOME/.bash_profile"
  [ -f "$RC" ] || RC="$HOME/.bashrc"
  [ -f "$RC" ] || RC="$HOME/.profile"
  if [ -n "${RC:-}" ] && [ -w "$RC" ]; then
    {
      echo ""
      echo "# dbr: 将 ~/bin 加入 PATH（由 install.sh 添加）"
      echo "export PATH=\"${LINK_DIR}:\$PATH\""
    } >> "$RC"
    path_added="$RC"
    ok "已在 ${RC} 追加 PATH 配置（重开终端或 source 后生效）。"
    warn "当前会话需执行: export PATH=\"${LINK_DIR}:\$PATH\""
  else
    warn "无法自动写入 shell 启动文件，请手动把 ${LINK_DIR} 加入 PATH："
    echo "      export PATH=\"${LINK_DIR}:\$PATH\""
  fi
fi

# ---- 校验 ----
echo
echo "  ── 校验 dbr --help ───────────────────────────────────────"
if [ -L "$LINK" ]; then
  if "$LINK" --help >/dev/null 2>&1; then
    ok "dbr --help 正常，部署完成。"
  else
    warn "dbr --help 未正常输出，请检查 ${LINK} 指向的 dbr.sh 与 Python 运行时。"
  fi
else
  info "未生成软链，跳过校验。"
fi

echo
echo "  ── 部署信息 ──────────────────────────────────────────────"
echo "  Python 运行时 : ${PY_BIN}"
echo "  软链          : ${LINK}"
echo "  打开方式      : 直接输入 dbr"
if in_path_now; then
  echo "  PATH          : 已包含 ${LINK_DIR}（当前终端即可用 dbr）"
elif [ -n "${path_added:-}" ]; then
  echo "  PATH          : 已写入 ${path_added}"
  echo "                  重开终端生效；当前会话可先执行:"
  echo "                    export PATH=\"${LINK_DIR}:\$PATH\""
else
  echo "  PATH          : ${LINK_DIR} 未在 PATH，请自行加入后再用 dbr"
fi
echo
ok "部署完成。"
