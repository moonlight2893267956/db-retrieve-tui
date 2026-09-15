# -*- coding: utf-8 -*-
from __future__ import unicode_literals
"""
配置加载封装（对齐 PHP 版 core/ConfigLoader.php）

职责：
  * load(path)      载入 Python 配置文件（返回 dict 的 .py）。
  * load_from_dict() 直接载入已解析 dict。
  * from_alias()     解析 shell `alias` 里的 mysql 连接作为数据源（本重构新增）。
  * get_* / get_all_* 便捷读取。
  * describe_db()    数据源摘要（脱敏，不显示密码）。
"""

import os
import re
import shlex
import subprocess

from .sharding_config import ShardingConfig
from .exceptions import DbrError


class ConfigLoader(object):
    def __init__(self):
        self._config = None
        self._loaded_file = ''

    # -- 载入 ------------------------------------------------------------
    def load(self, config_file):
        """载入 Python 配置文件（模块内需定义 CONFIG 变量为 dict）。"""
        if not os.path.exists(config_file):
            raise DbrError("配置文件不存在: %s" % config_file)

        if config_file.lower().endswith('.php'):
            raise DbrError(
                "Python 版无法解析 PHP 配置文件: %s\n"
                "  请使用 Python 配置（示例见 config/example_config.py），\n"
                "  或直接依赖 shell alias（默认行为，无需配置文件）。"
                % config_file
            )

        g = {'__file__': config_file, '__name__': '_dbr_config'}
        try:
            with open(config_file, 'r') as f:
                code = f.read()
            exec(compile(code, config_file, 'exec'), g)
        except SyntaxError as e:
            raise DbrError("配置文件语法错误 %s: %s" % (config_file, e))

        config = g.get('CONFIG')
        if config is None:
            raise DbrError(
                "配置文件未定义 CONFIG 变量（应为 dict）: %s" % config_file
            )
        self.load_from_dict(config)
        self._loaded_file = config_file
        return True

    def load_from_dict(self, config):
        self._config = ShardingConfig.instance()
        self._config.load_config_dict(config)
        return True

    # -- alias 复用 ------------------------------------------------------
    def from_alias(self, only_mysql=True):
        """
        解析 shell 中定义的 mysql alias，转成 databaseConfigs 注入配置。
        数据源名即 alias 名本身（如 paybase / cifdb / vipcifdb）。

        实现：读 `alias`（登录交互 shell，覆盖 ~/.bash_profile / ~/.bashrc），
        逐行解析形如
          alias paybase='mysql -A -h10.0.0.10 -P3306 -udb_user -pPWD'
        的连接串；密码可能带引号（如 -p'<pwd>&...'）。

        返回解析到的数据源名列表。
        """
        text = _read_shell_aliases()
        parsed = parse_mysql_aliases(text)

        if self._config is None:
            self._config = ShardingConfig.instance()

        added = []
        for name, cfg in parsed.items():
            if only_mysql and not cfg.get('host'):
                continue
            # 转成 databaseConfigs 结构（password 明文，来自 alias 本身）
            entry = {
                'host': cfg.get('host'),
                'port': cfg.get('port'),
                'username': cfg.get('user'),
                'password': cfg.get('password') or '',
            }
            if cfg.get('database'):
                entry['defaultDatabase'] = cfg['database']
            # 已通过 --config 显式配置的数据源优先，不被 alias 覆盖
            if not self._config.has_database_config(name):
                self._config.set_database_config(name, entry)
                added.append(name)
        return added

    # -- 便捷读取 --------------------------------------------------------
    def get_inner(self):
        return self._config

    def get_loaded_file(self):
        return self._loaded_file

    def get_database_names(self):
        return sorted(self._config.get_all_database_configs().keys())

    def get_rule_names(self):
        return sorted(self._config.get_all_sharding_rules().keys())

    def get_template_names(self):
        return sorted(self._config.get_all_query_templates().keys())

    def get_task_names(self):
        return sorted(self._config.get_all_tasks().keys())

    def get_database_config(self, db_name):
        return self._config.get_database_config(db_name)

    def get_rule(self, rule_name):
        return self._config.get_sharding_rule(rule_name)

    def get_template(self, template_name):
        return self._config.get_query_template(template_name)

    @staticmethod
    def describe_db(db_config):
        """数据源摘要（脱敏）：host:port  user  [口令状态]"""
        host = db_config.get('host', '')
        port = db_config.get('port', '')
        user = db_config.get('username', '')
        summary = "%s:%s  %s" % (host, port, user)
        if db_config.get('password') or db_config.get('passwordFile'):
            pass  # 口令就绪时不额外标注，保持简洁
        else:
            summary += '  · 无口令'
        return summary


# ---------------------------------------------------------------------------
# alias 读取与解析
# ---------------------------------------------------------------------------

def _read_shell_aliases():
    """
    读取当前 shell 的 alias 定义。
    优先 bash 登录交互 shell（部分 alias 定义在 ~/.bash_profile）。
    失败返回空串。
    """
    for argv in (['bash', '-lic', 'alias -p'], ['bash', '-ic', 'alias -p'], ['alias', '-p']):
        try:
            p = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
            )
            out, _err = p.communicate()
            if out:
                return out.decode('utf-8', 'replace')
        except (OSError, ValueError):
            continue
    return ''


_ALIAS_LINE_RE = re.compile(r"^\s*alias\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def parse_mysql_aliases(alias_text):
    """
    把 `alias name='mysql ...'` 解析为
      {name: {host, port, user, password, database}}
    仅返回含 mysql 且能解析出 host+user 的项。

    处理要点：alias 值可能含 shell 转义引号（如 -p'<pwd>&...' -> -p<pwd>&...）。
    """
    result = {}
    if not alias_text:
        return result

    for line in alias_text.splitlines():
        m = _ALIAS_LINE_RE.match(line)
        if not m:
            continue
        name = m.group(1)
        rhs = m.group(2).strip()

        # 去外层引号（'...' / "..." / $'...'）
        if rhs.startswith("$'") and rhs.endswith("'"):
            rhs = rhs[2:-1]
        elif (rhs.startswith("'") and rhs.endswith("'")) or \
             (rhs.startswith('"') and rhs.endswith('"')):
            rhs = rhs[1:-1]

        # 处理 shell 里 -p'pwd' 的拼接：形如  <pwd>'\''...'\''  ->  实际把引号去掉
        # 常见形态：-p'<pwd>' 在 alias 文本里是 -p'\''<pwd>'\''
        rhs = rhs.replace("'\\''", "'")
        rhs = rhs.replace('"\'"', "'")

        try:
            toks = shlex.split(rhs)
        except ValueError:
            toks = rhs.split()

        if not toks or os.path.basename(toks[0]) != 'mysql':
            continue

        cfg = {'host': None, 'port': None, 'user': None,
               'password': None, 'database': None}
        i = 1
        n = len(toks)
        while i < n:
            t = toks[i]
            if t in ('-h', '--host') and i + 1 < n:
                cfg['host'] = toks[i + 1]; i += 2
            elif t.startswith('-h') and len(t) > 2:
                cfg['host'] = t[2:]; i += 1
            elif t in ('-P', '--port') and i + 1 < n:
                cfg['port'] = toks[i + 1]; i += 2
            elif t.startswith('-P') and len(t) > 2:
                cfg['port'] = t[2:]; i += 1
            elif t in ('-u', '--user') and i + 1 < n:
                cfg['user'] = toks[i + 1]; i += 2
            elif t.startswith('-u') and len(t) > 2:
                cfg['user'] = t[2:]; i += 1
            elif t in ('-p', '--password') and i + 1 < n:
                cfg['password'] = toks[i + 1]; i += 2
            elif t.startswith('-p') and len(t) > 2:
                cfg['password'] = t[2:]; i += 1
            elif t in ('-D', '--database') and i + 1 < n:
                cfg['database'] = toks[i + 1]; i += 2
            elif t.startswith('--database='):
                cfg['database'] = t.split('=', 1)[1]; i += 1
            else:
                i += 1

        if cfg['host'] and cfg['user']:
            result[name] = cfg
    return result
