#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import unicode_literals
"""
dbr.py - MySQL 分库分表交互查询工具入口（Python 2.7 版）

用法：
  python dbr.py                         # 交互式，数据源来自 shell alias
  python dbr.py --db=paybase            # 跳过选数据源
  python dbr.py --db=paybase --query='库.表 字段|where|LIMIT'  # 一次性查询
  python dbr.py --config=<配置.py>       # 额外加载配置（规则/自定义数据源）
  python dbr.py --no-alias              # 不使用 alias
  python dbr.py --help

运行环境：Python 2.7（目标机器自带）；无需第三方模块；数据库访问走
        系统 mysql 客户端（/bin/mysql）子进程。
"""

import os
import sys

# 保证 stdout/stderr 以 UTF-8 输出（Python 2 下默认 ascii，中文会报
# UnicodeEncodeError）。仅对标准流包裹一层 UTF-8 writer。
def _setup_utf8_stdio():
    if sys.version_info[0] != 2:
        return
    import codecs
    for name in ('stdout', 'stderr'):
        stream = getattr(sys, name)
        enc = getattr(stream, 'encoding', None)
        if enc is not None and enc.lower().replace('-', '') == 'utf8':
            continue
        try:
            buf = getattr(stream, 'buffer', stream)
            setattr(sys, name, codecs.getwriter('utf-8')(buf))
        except Exception:
            pass


_setup_utf8_stdio()

# 保证以「任意工作目录」运行都能 import 包
DBR_ROOT = os.path.dirname(os.path.abspath(__file__))
if DBR_ROOT not in sys.path:
    sys.path.insert(0, DBR_ROOT)

from core.config_loader import ConfigLoader
from core.sql_client import SqlClient, find_mysql_client
from core.exceptions import DbrError
from controller.tui import Tui
from controller.repl import Repl

HELP_TEXT = u"""dbr - MySQL 分库分表查询工具（Python 版）
----------------------------------
用法:
  python dbr.py                          # 交互式：数据源来自 shell alias
  python dbr.py --db=paybase             # 交互式：跳过选数据源
  python dbr.py --db=paybase --query='demo_db_00.t_user_setting_00_3 F_user_id=1000000003'
                                         # 一次性非交互查询（直接出结果）
  python dbr.py --config=<配置.py>        # 额外加载配置（分库分表规则/自定义数据源）
  python dbr.py --no-alias               # 不使用 alias，仅用 --config 数据源
  python dbr.py --no-clear               # 关闭「步骤切换清屏」（保留全部滚动历史）
  python dbr.py --db=statdb --field=F_log_id --file=ids.csv \\
      --query='demo_stat_db.t_verify_20260903 F_log_id,F_front_bank_code,F_back_bank_code'
                                         # 批量值清单：拿 ids.csv 里的 log_id 分批 IN 查询
  python dbr.py --help

批量值清单查询（值文件 + 字段 → WHERE 字段 IN (...) 分批）:
  python dbr.py --db=<数据源> --query='<库>.<表> [字段 | | LIMIT | order by ..]' \\
      --field=<字段名> --file=<值文件> [--batch-size=1000] [--no-header|--has-header] [--column=0]
  * 值文件每行一个值；有无表头自动识别，可用 --no-header/--has-header 强制；
  * 内部按 --batch-size（默认 1000）分批生成 IN，避免单条 SQL 过大；
  * 结果打印「输入 N / 命中 X / 未命中 Y / 结果 M 行」，并按表×批汇总；
  * 交互式：在「查询方式」提示处输 i 同样可进入批量模式。

值清单文件目录（--file 的默认查找位置）:
  * 默认基准目录为 <dbr>/data/；把清单文件放进去，交互时会列成菜单供选择，
    无需输入完整路径；--file=<纯文件名> 也会自动到该目录查找。
  * 用 --data-dir=<目录> 可指定其它基准目录（相对路径相对 dbr 根目录）。
  例:  python dbr.py --db=paybase --field=F_user_id --file=ids.csv \\
           --query='demo_db_00.t_user_setting_{db}_{table} F_user_id'

交互流程（选库与选表分离）:
  ① 选数据源 → ② 选库(逻辑库组/单库) → ③ 选表(逻辑表组/单表)
  → ④ 填条件(字段[默认*] → where[回车不加] → LIMIT[默认100] → 高级子句[回车跳过])
  → ⑤ 预览即执行(回车) → 结果(快捷: s 排序 / v 行详情 / e 导出 / q 返回)
  整组遍历：选库/选表时选中“组模板”后回车即可。
  菜单通用键: 0 或 b=返回, q=退出, p/n=翻页, 回车=默认。

高级子句（排序 / 分组 / 聚合）:
  在“高级子句”提示行输入 o / g / h 可分别编辑：
    o  ORDER BY   如  F_id 或 F_id DESC, F_time
    g  GROUP BY   如  F_a, F_b（配合聚合函数）
    h  HAVING     如  COUNT(*) > 1
  回车跳过即不添加。多表遍历时，COUNT/SUM/MAX/MIN/AVG 会跨分表**合并为全局聚合**；
  GROUP_CONCAT、COUNT(DISTINCT ...)、STDDEV 等无法合并，多表时会提示缩到单表。

界面（步骤切换清屏，默认开; 脚本/管道自动关闭）:
  每进入新步骤会清屏并重画「头部 + 面包屑 + 上一步摘要」，界面更聚焦；
  上一步的关键上下文（数据源/库/表/目标数/缩表结论/上次结果）以摘要行保留在顶部。
  在 TTY 下默认开启；输出重定向或管道运行时自动关闭，保证完整可回溯。

--query 语法（一次性查询）:
  "库.表 [字段 | where | LIMIT | order by .. | group by .. | having ..]"
  例:  "demo_db_00.t_user_setting_00_3 F_user_id=1000000003"
       "demo_db_{db}.t_x_{db}_{table} F_id,F_user_id | id=1 | 50"
       "demo_db_{db}.t_x_{db}_{table} F_user_id, COUNT(*) AS c | | NONE | group by F_user_id | order by c DESC"

数据源（默认）:
  直接读取当前 shell 的 mysql alias，例如：
    alias paybase='mysql -A -h10.0.0.10 -P3306 -udb_user -pPASSWORD'
  数据源名即 alias 名；无需任何配置文件。
"""


def print_help():
    try:
        sys.stdout.write(HELP_TEXT)
    except UnicodeEncodeError:
        sys.stdout.write(HELP_TEXT.encode('utf-8'))
    sys.stdout.write('\n')


def _is_py2():
    return sys.version_info[0] == 2


def parse_args(argv):
    opts = {'config': None, 'no_alias': False, 'help': False,
            'db': None, 'query': None, 'clear': None,
            'file': None, 'field': None, 'batch_size': None,
            'has_header': None, 'column': None, 'data_dir': None}
    for a in argv[1:]:
        if a in ('--help', '-h'):
            opts['help'] = True
        elif a.startswith('--config='):
            opts['config'] = a.split('=', 1)[1]
        elif a == '--no-alias':
            opts['no_alias'] = True
        elif a == '--from-alias':
            # 兼容旧参数：alias 现在是默认行为，此参数无副作用
            opts['no_alias'] = False
        elif a == '--clear':
            opts['clear'] = True
        elif a == '--no-clear':
            opts['clear'] = False
        elif a.startswith('--db='):
            opts['db'] = a.split('=', 1)[1]
        elif a.startswith('--query='):
            opts['query'] = a.split('=', 1)[1]
        elif a.startswith('--file='):
            opts['file'] = a.split('=', 1)[1]
        elif a.startswith('--field='):
            opts['field'] = a.split('=', 1)[1]
        elif a.startswith('--batch-size='):
            opts['batch_size'] = a.split('=', 1)[1]
        elif a == '--no-header':
            opts['has_header'] = False
        elif a == '--has-header':
            opts['has_header'] = True
        elif a.startswith('--column='):
            opts['column'] = a.split('=', 1)[1]
        elif a.startswith('--data-dir='):
            opts['data_dir'] = a.split('=', 1)[1]
    return opts


def main(argv):
    opts = parse_args(argv)

    if opts['help']:
        print_help()
        return 0

    # 提前探测 mysql 客户端，缺失时明确报错
    if not find_mysql_client():
        sys.stderr.write(
            "[错误] 未找到可用的 mysql 客户端，dbr 无法连接数据库。\n"
            "  请确认系统安装了 mysql/mariadb 客户端（/bin/mysql），或加入 PATH。\n")
        return 1

    loader = ConfigLoader()

    # 1) 显式 --config：加载自定义 Python 配置（含分库分表规则等）
    if opts['config']:
        config_file = opts['config']
        if config_file[0] != '/':
            config_file = os.path.join(DBR_ROOT, config_file)
        try:
            loader.load(config_file)
        except DbrError as e:
            sys.stderr.write('[错误] %s\n' % e)
            return 1

    # 2) 默认直接从 shell alias 注入数据源（最简单：无需任何配置文件）
    #    --no-alias 可关闭（仅在需要用 --config 里同名数据源时）。
    if not opts['no_alias']:
        try:
            added = loader.from_alias()
            if added:
                sys.stderr.write('[信息] 数据源来自 shell alias: %s\n' % ', '.join(added))
            elif not opts['config']:
                sys.stderr.write(
                    '[错误] 未在当前 shell 中找到 mysql alias 数据源，也未指定 --config。\n'
                    '  请确认已在 ~/.bash_profile 配置形如下面的 alias：\n'
                    "    alias paybase='mysql -hHOST -PPORT -uUSER -pPASSWORD'\n")
                return 1
        except Exception as e:
            sys.stderr.write('[警告] 解析 alias 失败: %s\n' % e)

    # 兜底：若仍无任何数据源，给出提示
    if not loader.get_database_names():
        sys.stderr.write('[错误] 没有任何可用数据源（alias 或 --config）。\n')
        return 1

    sql = SqlClient()
    # 步骤切换清屏（默认开；非 TTY 自动失效）；--no-clear 关闭，--clear 显式开启
    if opts['clear'] is not None:
        Tui.set_clear(opts['clear'])
    try:
        repl = Repl(loader, DBR_ROOT, sql, preferred_db=opts['db'],
                    data_dir=opts.get('data_dir'))
        # 批量值清单（--file 与 --field 同时给出）：非交互
        if opts['file'] is not None or opts['field'] is not None:
            if not opts['file'] or not opts['field']:
                sys.stderr.write(
                    '[错误] 批量模式需同时指定 --file=<值文件> 与 --field=<字段名>。\n')
                return 1
            db_name = opts['db']
            if not db_name:
                names = loader.get_database_names()
                if len(names) == 1:
                    db_name = names[0]
                else:
                    sys.stderr.write(
                        '[错误] 使用 --file/--field 时需用 --db=<数据源> 指定数据源。\n'
                        '  可选：%s\n' % ', '.join(names))
                    return 1
            ok = repl.run_bulk_query_once(
                db_name, opts.get('query'), opts['file'], opts['field'],
                batch_size=opts.get('batch_size'),
                has_header=opts.get('has_header'),
                column=opts.get('column'))
            return 0 if ok else 1
        # 一次性查询：--query（非交互）
        if opts['query'] is not None:
            db_name = opts['db']
            if not db_name:
                names = loader.get_database_names()
                if len(names) == 1:
                    db_name = names[0]
                else:
                    sys.stderr.write(
                        '[错误] 使用 --query 时需用 --db=<数据源> 指定数据源。\n'
                        '  可选：%s\n' % ', '.join(names))
                    return 1
            ok = repl.run_query_once(db_name, opts['query'])
            return 0 if ok else 1
        repl.run()
    except KeyboardInterrupt:
        sys.stderr.write('\n[中断] 已退出。\n')
        return 130
    except DbrError as e:
        sys.stderr.write('\n[错误] %s\n' % e)
        return 1
    except Exception as e:
        # 兜底：任何未预期异常都给可读信息，绝不向终端喷 traceback
        # （设 DBR_DEBUG=1 可打印完整堆栈，便于定位）
        import traceback
        if os.environ.get('DBR_DEBUG'):
            traceback.print_exc()
        try:
            msg = u'%s' % e
        except Exception:
            msg = repr(e)
        sys.stderr.write(u'\n[内部错误] %s: %s\n' % (type(e).__name__, msg))
        sys.stderr.write(u'  可设 DBR_DEBUG=1 复跑以查看完整堆栈。\n')
        return 1
    finally:
        sql.clear()
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
