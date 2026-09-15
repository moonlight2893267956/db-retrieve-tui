# -*- coding: utf-8 -*-
from __future__ import unicode_literals
"""
SQL 客户端（对齐 PHP 版 core/SQL.php）

关键差异：Python 2.7.5 环境没有 MySQLdb/_mysql/pymysql，也无可用的 pip，
因此改用 subprocess 调用系统 mysql 客户端（/bin/mysql，MariaDB 5.5 客户端）。

设计要点：
  * 每次查询起一个 mysql 子进程（-B 批处理 TSV + -N 去表头），用 csv 解析。
  * 密码通过环境变量 MYSQL_PWD 传入，不出现在 argv/ps 中。
  * SQL 通过 -e 传入；连接使用 --default-character-set=utf8，让 MySQL 侧做
    字符集转换（对齐 PHP 版「latin1 连接 + 展示层 GBK→UTF8」的最终效果）。
  * 只读账号约定不变；不打印密码。

Python 2.7 兼容。
"""

import os
import csv
import subprocess

from .exceptions import QueryError

try:
    from collections import OrderedDict as _OrderedDict
except ImportError:  # pragma: no cover
    _OrderedDict = dict

try:
    from StringIO import StringIO  # Python 2
except ImportError:  # pragma: no cover - 仅 Python 3 兜底
    from io import StringIO


# ---------------------------------------------------------------------------
# 运行时（mysql 客户端）探测
# ---------------------------------------------------------------------------

# 候选 mysql 客户端路径（按优先级）
MYSQL_CLIENT_CANDIDATES = [
    '/bin/mysql',
    '/usr/bin/mysql',
    '/usr/local/bin/mysql',
    'mysql',  # 走 PATH
]

_MYSQL_CLIENT_CACHE = {'path': None, 'checked': False}


def find_mysql_client():
    """探测可用的 mysql 客户端路径；找不到返回 None（结果缓存）。"""
    if _MYSQL_CLIENT_CACHE['checked']:
        return _MYSQL_CLIENT_CACHE['path']

    found = None
    for cand in MYSQL_CLIENT_CANDIDATES:
        if os.path.isabs(cand):
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                found = cand
                break
        else:
            # 非绝对路径：在 PATH 中查找
            p = _which(cand)
            if p:
                found = p
                break

    _MYSQL_CLIENT_CACHE['checked'] = True
    _MYSQL_CLIENT_CACHE['path'] = found
    return found


def _which(name):
    """极简 which 实现（避免依赖 shutil.which，Python 2.7 无）。"""
    path = os.environ.get('PATH', '')
    for d in path.split(os.pathsep):
        if not d:
            continue
        full = os.path.join(d, name)
        if os.path.isfile(full) and os.access(full, os.X_OK):
            return full
    return None


def _b(s):
    """把 unicode/str 转成字节 str（Python 2 的 subprocess/env 需要 bytes）。"""
    if s is None:
        return None
    try:
        if isinstance(s, bytes):
            return s
    except NameError:  # pragma: no cover
        pass
    try:
        return s.encode('utf-8')
    except AttributeError:
        return str(s)


def require_mysql_client():
    """确保 mysql 客户端可用，否则抛出 QueryError。"""
    p = find_mysql_client()
    if not p:
        raise QueryError(
            "未找到可用的 mysql 客户端（已尝试: %s）。\n"
            "  请确认系统安装了 mysql/mariadb 客户端，或将其加入 PATH。"
            % ', '.join(MYSQL_CLIENT_CANDIDATES)
        )
    return p


# ---------------------------------------------------------------------------
# 连接配置 & SQL 执行
# ---------------------------------------------------------------------------

class SqlClient(object):
    """
    对齐 PHP 版 SQL 单例语义：持有各连接的配置（在内存），实际执行走子进程。
    """

    def __init__(self):
        # dbName -> {host, port, username, password}
        self._configs = {}

    # -- 配置 ------------------------------------------------------------
    def init_config(self, db_name, host, port, username, password):
        self._configs[db_name] = {
            'host': host,
            'port': port,
            'username': username,
            'password': password,
        }

    def has_config(self, db_name):
        return db_name in self._configs

    def get_config(self, db_name):
        if db_name not in self._configs:
            raise QueryError(
                "数据库连接配置不存在: %s。已配置的连接: %s"
                % (db_name, ', '.join(sorted(self._configs.keys())))
            )
        return self._configs[db_name]

    def clear(self):
        self._configs = {}

    # -- 执行 ------------------------------------------------------------
    def query(self, db_name, sql, timeout=30, database=None):
        """
        执行 SQL，返回 list[dict]（每行一个 {列名: 值}）。
        连接/执行失败时抛出 QueryError（对齐 PHP 版 query() 返回 false 的语义，
        由上层 QueryExecutor 决定是否重试/记错）。

        列名取自 mysql batch 输出的首行表头（不使用 -N），从而与 PHP mysqli
        fetch_assoc 的关联数组行为一致。

        参数:
            db_name:  连接配置名（databaseConfigs 的 key）
            sql:      待执行 SQL
            timeout:  子进程超时秒数（对齐 PHP QueryExecutor::$timeout 语义）
            database: 可选；指定后连接时即 `-D <database>` 选库（兼容要求先选库的
                      MySQL 分支，如 5.0 DTM；对新版本服务器无副作用）
        """
        cfg = self.get_config(db_name)
        _headers, rows = self._run_mysql(cfg, sql, timeout=timeout, database=database)
        return rows

    def query_headers(self, db_name, sql, timeout=30, database=None):
        """执行 SQL 并返回 (headers, rows)。headers 为列名列表。"""
        cfg = self.get_config(db_name)
        return self._run_mysql(cfg, sql, timeout=timeout, database=database)

    def query_scalar_list(self, db_name, sql, timeout=30, database=None):
        """
        便捷方法：取单列结果的字符串列表（用于 SHOW DATABASES / SHOW TABLES）。
        """
        rows = self.query(db_name, sql, timeout=timeout, database=database)
        out = []
        for row in rows:
            if not row:
                continue
            # 取该行第一个值（按列序）
            for k in sorted(row.keys(), key=_col_sort_key):
                v = row[k]
                if v is not None:
                    out.append(v)
                break
        return out

    # -- 内部：调用 mysql 客户端 -----------------------------------------
    def _run_mysql(self, cfg, sql, timeout=30, database=None):
        client = require_mysql_client()

        host = cfg.get('host')
        port = cfg.get('port')
        user = cfg.get('username')
        password = cfg.get('password')

        if not host or not port or not user:
            raise QueryError(
                "数据库连接配置不完整 (host, port, username 不能为空): %s"
                % cfg
            )

        argv = [
            _b(client),
            '-A',                       # --no-auto-rehash，加速启动
            '-h', _b(host),
            '-P', _b(port),
            '-u', _b(user),
            '--default-character-set=utf8',
            '-B',                       # batch：TSV 输出，禁用交互（保留表头）
            '--connect-timeout=10',
        ]
        # 连接时先选库（-D）：部分 MySQL 分支（如 5.0 DTM）要求连接必须已选库，
        # 否则 `SHOW TABLES FROM x` 会报 "ERROR 5042 no database"。
        if database:
            argv += ['-D', _b(database)]
        argv += ['-e', _b(sql)]

        env = dict(os.environ)
        # 密码走环境变量（不进 argv/ps）；空密码时显式置空触发无密码连接
        env[_b('MYSQL_PWD')] = _b(password if password is not None else '')

        try:
            p = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                close_fds=True,
            )
        except OSError as e:
            raise QueryError("无法启动 mysql 客户端: %s" % e)

        # Python 2.7.5 的 communicate() 不接受 timeout 参数；
        # 超时由上层按需处理，这里直接阻塞读取输出。
        out, err = p.communicate()
        rc = p.returncode

        # 解码：优先 UTF-8（--default-character-set=utf8 已保证 MySQL 侧输出 UTF-8），
        # 失败再按 GBK/GB18030 兜底（数据可能残留 GBK 字节流）。
        stdout_text = _safe_decode(out)
        stderr_text = _safe_decode(err)

        if rc != 0:
            msg = stderr_text.strip() or stdout_text.strip()
            raise QueryError(u"mysql 执行失败 (rc=%s): %s" % (rc, msg))

        return _parse_tsv(stdout_text)


# ---------------------------------------------------------------------------
# 解析 & 解码工具
# ---------------------------------------------------------------------------

def _col_sort_key(colname):
    """col0/col1/... 按数字部分排序，保证取「第一列」语义正确。"""
    if colname.startswith('col') and colname[3:].isdigit():
        return int(colname[3:])
    return colname


def _parse_tsv(text):
    """
    解析 mysql -B 输出的 TSV（制表符分隔，行尾 \\n，首行为列名表头）。
    注意 mysql batch 模式对特殊字符会做转义（\\t, \\n, \\\\ 等），这里手动反转义。
    返回 (headers, rows)：headers 为列名列表，rows 为 list[dict]{列名: 值}。
    """
    if text == '':
        return [], []

    # 增加字段上限，避免超宽行报错
    try:
        csv.field_size_limit(1024 * 1024 * 512)
    except Exception:
        pass

    # 手动按 TAB 切分并反转义（避免 py2 csv 的 unicode delimiter 限制）。
    # text 已是 unicode（由 _safe_decode 解码）。
    lines = text.split('\n')
    # 去掉末尾由行尾 \n 产生的空元素
    if lines and lines[-1] == '':
        lines.pop()

    headers = None
    rows = []
    for line in lines:
        raw = line.split('\t')
        cells = [_unescape_mysql_batch(c) for c in raw]
        if headers is None:
            headers = cells
            continue
        row = _OrderedDict()
        for i, cell in enumerate(cells):
            key = headers[i] if i < len(headers) else 'col%d' % i
            row[key] = cell
        rows.append(row)
    if headers is None:
        headers = []
    return headers, rows


def _unescape_mysql_batch(s):
    """
    还原 mysql --batch 输出中的转义序列：
      \\t -> TAB, \\n -> LF, \\\\ -> \\, \\0 -> NUL
    mysql batch 模式会对字段内真正的 TAB/LF/\\/NUL 转义。
    """
    if '\\' not in s:
        return s
    out = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == '\\' and i + 1 < n:
            nxt = s[i + 1]
            if nxt == 't':
                out.append('\t'); i += 2; continue
            if nxt == 'n':
                out.append('\n'); i += 2; continue
            if nxt == '\\':
                out.append('\\'); i += 2; continue
            if nxt == '0':
                out.append('\0'); i += 2; continue
        out.append(c)
        i += 1
    return ''.join(out)


def _safe_decode(raw):
    """
    把 mysql 客户端返回的字节安全解码为 unicode 文本。
    对齐 PHP TableRenderer::safeDecode 思路：
      已是合法 UTF-8 -> 原样；否则按 GBK 转；再失败按 GB18030；都失败用替换字符。
    输入可能是 str(PY2 bytes) 或 unicode。
    """
    if raw is None:
        return u''
    if isinstance(raw, unicode):  # noqa: F821  (Python 2 内置)
        return raw
    if not isinstance(raw, bytes):
        return unicode(raw)  # noqa: F821
    if raw == b'':
        return u''
    # 1) 试 UTF-8
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        pass
    # 2) 试 GBK
    try:
        return raw.decode('gbk')
    except UnicodeDecodeError:
        pass
    # 3) 试 GB18030
    try:
        return raw.decode('gb18030')
    except UnicodeDecodeError:
        return raw.decode('utf-8', 'replace')
