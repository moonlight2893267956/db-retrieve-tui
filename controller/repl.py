# -*- coding: utf-8 -*-
from __future__ import unicode_literals
"""
交互主循环（精简版）

一次查询仅 3~4 次交互：
  ① 选数据源 → ② 一次定位目标(库×表) → ③ 一次填条件(字段|where|LIMIT)
  → ④ 预览即执行(回车) → 结果(单字符快捷：s 排序 / e 导出 / q 返回)
"""

import os
import re
import sys
import time

try:
    from collections import OrderedDict as _OrderedDict
except ImportError:  # pragma: no cover
    _OrderedDict = dict

from .tui import Tui, SENTINEL_EOF
from .table_renderer import TableRenderer
from .db_connector import DbConnector

from core.route_computation import RouteComputation
from core.query_executor import QueryExecutor
from core.data_aggregator import DataAggregator
from core.result_exporter import ResultExporter
from core.sql_builder import QueryParts, build_sql
from core.exceptions import RowLimitExceededError, DbrError


DEFAULT_MAX_ROWS = 1000

# 批量值清单查询：默认每批 IN 数量、查询次数提醒阈值
DEFAULT_BULK_BATCH_SIZE = 1000
BULK_CONFIRM_THRESHOLD = 500

# 常见中文全角符号 -> 半角（SQL 里用全角会语法报错）
_FULLWIDTH_MAP = {
    u'\u2018': u"'", u'\u2019': u"'",   # ‘ ’
    u'\u201c': u'"', u'\u201d': u'"',   # “ ”
    u'\uff07': u"'", u'\uff02': u'"',   # ＇ ＂
    u'\uff1d': u'=',                     # ＝
    u'\uff08': u'(', u'\uff09': u')',   # （ ）
    u'\uff0c': u',',                     # ，
    u'\uff1b': u';',                     # ；
    u'\u3002': u'.',                     # 。
    u'\uff1f': u'?',                     # ？（用于「查看表结构」触发）
}


def normalize_chars(text):
    """把输入里的中文全角符号规范化为半角（SQL 安全）。仅做字符替换，不改语义。"""
    if not text:
        return text
    out = []
    for ch in text:
        out.append(_FULLWIDTH_MAP.get(ch, ch))
    return u''.join(out)


# collect 聚合时追加的分库分表标识列（展示时收敛，导出时保留）
_META_COLS = ('db_index', 'table_index', 'table_name', 'date')

# 跨表 AVG 合并用的内部辅助列前缀（SUM(x)/COUNT(x)），仅供合并计算，不对外展示
_HELPER_PREFIXES = (u'__sum_', u'__cnt_')


def _is_helper_col(name):
    n = name or u''
    return any(n.startswith(p) for p in _HELPER_PREFIXES)


def _escape_sql_literal(value):
    """转义 SQL 字符串字面量（' -> ''，\\ -> \\\\），用于库名/表名等值。"""
    if value is None:
        return u''
    if isinstance(value, bytes):
        try:
            s = value.decode('utf-8')
        except UnicodeDecodeError:
            s = value.decode('gbk', 'replace')
    else:
        s = u'%s' % value
    s = s.replace(u'\\', u'\\\\')
    s = s.replace(u"'", u"''")
    return s


def _first_col(row, name):
    """从结果行 dict 里按列名取第一个匹配值（大小写不敏感）。找不到返回 None。"""
    if not row:
        return None
    want = name.lower()
    for k in row.keys():
        kk = k
        if isinstance(kk, bytes):
            try:
                kk = kk.decode('utf-8')
            except UnicodeDecodeError:
                kk = kk.decode('gbk', 'replace')
        if kk.lower() == want:
            return row[k]
    return None


# 表结构展示的列，以及键值的中文映射
_STRUCT_HEADERS = (u'字段', u'类型', u'允许空', u'键', u'默认', u'注释')
_STRUCT_KEY_MAP = {u'PRI': u'主键', u'MUL': u'索引', u'UNI': u'唯一'}


def _as_text(v):
    if v is None:
        return u''
    if isinstance(v, bytes):
        try:
            return v.decode('utf-8')
        except UnicodeDecodeError:
            return v.decode('gbk', 'replace')
    return u'%s' % v


def _struct_display_row(tup):
    """把 (field,type,nullable,key,default,comment) 转成展示用 dict（键为中文表头）。"""
    vals = list(tup) + [None] * 6
    field, ctype, nullable, key, default, comment = vals[:6]
    d = _as_text(default)
    if d.upper() == u'NULL':
        d = u'-'
    return {
        _STRUCT_HEADERS[0]: _as_text(field),
        _STRUCT_HEADERS[1]: _as_text(ctype),
        _STRUCT_HEADERS[2]: u'是' if _as_text(nullable).upper() == u'YES' else u'否',
        _STRUCT_HEADERS[3]: _STRUCT_KEY_MAP.get(_as_text(key).upper(), u'-'),
        _STRUCT_HEADERS[4]: d,
        _STRUCT_HEADERS[5]: _as_text(comment),
    }


def _struct_signature(rows):
    """结构签名：用于判断多张分表结构是否一致（可哈希比较）。"""
    return tuple(tuple(_as_text(c) for c in r) for r in rows)


def _ensure_field_in_fields(fields, field):
    """
    确保 SELECT 字段里包含 field（大小写不敏感），否则追加。
    用于批量值清单查询的命中统计（命中值需从结果行取该字段）。
    fields 为 '*' 时原样返回（已含所有列）。
    """
    if fields is None or fields.strip() in ('', '*'):
        return fields
    cols = [c.strip() for c in fields.split(',')]
    lower = [c.lower() for c in cols]
    if field.lower() in lower:
        return fields
    return fields + u', ' + field


def _bulk_hit_stats(rows, field, values):
    """
    统计命中的字段值数。
    返回 (hit_count, None)；无法判定（字段不在结果列）时返回 (None, None)。

    字段名在结果行里可能大小写不同，做大小写不敏感匹配。
    """
    if not rows:
        return 0, None
    # 找结果行里对应的字段名
    sample = rows[0]
    actual = None
    for k in sample.keys():
        if (k or u'').lower() == field.lower():
            actual = k
            break
    if actual is None:
        return None, None
    hit = set()
    for r in rows:
        v = r.get(actual)
        if v is None:
            continue
        sv = v.strip() if hasattr(v, 'strip') else u'%s' % v
        if sv != '':
            hit.add(sv)
    return len(hit), None


def display_view(headers, rows):
    """
    展示层列收敛：来源标识列 (db_index/table_index/table_name/date) 不进表格。
      * 全部行同一张表：去掉来源列，返回 note（顶部用一行灰字注明来源）；
      * 多来源行：三列合一列为 `table`（跨库同名表自动带 db_index 前缀）。
    内部辅助列 (__sum_x/__cnt_x) 任何情况下都不展示（跨表 AVG 合并的中间列）。
    返回 (disp_headers, disp_rows, note)。排序/导出仍用原始 rows（信息不丢）。
    """
    # 先剥离内部辅助列（不参与展示，也不影响来源判定）
    headers = [h for h in headers if not _is_helper_col(h)]
    rows = [dict((k, v) for k, v in r.items() if not _is_helper_col(k)) for r in rows]
    meta = [h for h in headers if h in _META_COLS]
    business = [h for h in headers if h not in _META_COLS]
    if not meta or not business:
        return headers, rows, None

    sources = []
    for r in rows:
        key = (r.get('db_index', u''), r.get('table_name', u''), r.get('date', u''))
        if key not in sources:
            sources.append(key)

    if len(sources) <= 1:
        disp_rows = []
        for r in rows:
            nr = _OrderedDict()
            for h in business:
                nr[h] = r.get(h, u'')
            disp_rows.append(nr)
        note = rows[0].get('table_name') or rows[0].get('date') or None
        return business, disp_rows, note

    # 多来源：是否出现「同名表在不同库」（此时来源列带 db_index 前缀消歧）
    name_set = set(s[1] for s in sources)
    collide = len(name_set) < len(sources)
    disp_headers = business + [u'table']
    disp_rows = []
    for r in rows:
        nr = _OrderedDict()
        for h in business:
            nr[h] = r.get(h, u'')
        tn = r.get('table_name') or r.get('date') or u''
        if collide and r.get('db_index', u'') != u'':
            tn = u'%s/%s' % (r.get('db_index'), tn)
        nr[u'table'] = tn
        disp_rows.append(nr)
    return disp_headers, disp_rows, None


class Repl(object):
    def __init__(self, loader, root_dir, sql_client, preferred_db=None, data_dir=None):
        self.loader = loader
        self.rootDir = root_dir.rstrip('/')
        self.sql = sql_client
        self.preferred_db = preferred_db
        self.dataDir = data_dir  # 基准数据目录（None=默认 <root>/data）
        self.db = DbConnector(loader, root_dir, sql_client)
        self.route = RouteComputation()
        self.executor = QueryExecutor(sql_client)
        self.executor.set_retry_count(3)
        self.executor.set_timeout(30)
        self.aggregator = DataAggregator()
        self.renderer = TableRenderer()
        # 来源列区分位在尾部（…setting_78_0），截断时保留尾部
        self.renderer.tailKeepColumns = set([u'table'])
        self.exporter = ResultExporter()
        self.exporter.set_max_rows(DEFAULT_MAX_ROWS)
        # 步骤切换重画用的上下文（清屏后保留在顶部，避免丢上下文）
        self.ctx = {
            'db': '',          # 数据源
            'db_template': '', # 库名/库模板
            'table_template': '',  # 表名/表模板
            'targets': 0,      # 目标表数
            'shrink': '',      # 缩表结论
            'result': '',      # 最近一次结果摘要
        }

    # ------------------------------------------------------------------
    # 步骤切换：清屏 + 重画上下文摘要
    # ------------------------------------------------------------------
    def _crumbs(self, step=''):
        parts = [self.ctx['db'] or u'-']
        if self.ctx['db_template']:
            parts.append(self.ctx['db_template'])
        if self.ctx['table_template']:
            parts.append(self.ctx['table_template'])
        if step:
            parts.append(u'[%s]' % step)
        return parts

    def _summary(self):
        lines = []
        if self.ctx['targets']:
            scope = u'占位符遍历' if self.ctx['targets'] > 1 else u'单表'
            lines.append(u'目标  %d 张表  ·  %s' % (self.ctx['targets'], scope))
        if self.ctx['shrink']:
            lines.append(self.ctx['shrink'])
        if self.ctx['result']:
            lines.append(self.ctx['result'])
        return lines

    def screen(self, step=''):
        """步骤切换锚点：清屏并重画头部/面包屑/上一步摘要（非 TTY 时自动降级）。"""
        Tui.anchor(crumbs=self._crumbs(step), summary=self._summary())

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def run(self):
        while True:
            self.guided_query_flow()
            Tui._out('\n')
            ans = Tui.readline('  ' + Tui.dim('› 回车继续 · q 退出') + ': ')
            if ans == SENTINEL_EOF:
                return
            if ans.strip().lower() in ('q', 'quit', 'exit', 'n', 'no'):
                return

    # ------------------------------------------------------------------
    # 向导式查询（选库 → 选表 → 一行条件 → 预览执行）
    # ------------------------------------------------------------------
    def guided_query_flow(self):
        # 重置本次查询的上下文（结果摘要跨步骤保留，数据源若已连接沿用）
        self.ctx['db_template'] = ''
        self.ctx['table_template'] = ''
        self.ctx['targets'] = 0
        self.ctx['shrink'] = ''
        self.ctx['result'] = ''

        # --db 预设：跳过选数据源
        if self.preferred_db and self.preferred_db in self.loader.get_database_names():
            db_name = self.preferred_db
            if not self.db.ensure_connected(db_name):
                return
            self.ctx['db'] = db_name
            Tui.ok('已连接 %s' % db_name)
        else:
            db_name = self.db.select_and_connect()
            if db_name is False:
                return
            self.ctx['db'] = db_name

        # ① 两步定位目标：先选库 → 再选表（整组遍历在选到“库组/表组模板”时生效）
        self.screen(u'选库')
        dbs = []
        db_template = self.pick_database(db_name, dbs)
        if db_template == '':
            return
        self.ctx['db_template'] = db_template
        base_db = self.first_matching_db(db_template, dbs)
        if base_db is None:
            Tui.err("库模板 '%s' 没有匹配的库" % db_template)
            return

        # 库模板含占位符：明确提示将遍历多少个库（避免误以为只查某个库）
        if '{' in db_template:
            n_dbs = len(self._match_dbs(db_template, dbs))
            self.screen(u'选表')
            Tui.hint(u'库模板 %s 将遍历 %d 个库（表清单取自 %s）' % (
                db_template, n_dbs, base_db))
        else:
            self.screen(u'选表')

        tables = []
        table_template = self.pick_table(db_name, base_db, tables)
        if table_template == '':
            return
        self.ctx['table_template'] = table_template

        efail = {}
        targets = self.expand_targets_by_template(
            db_template, dbs, table_template, tables, db_name, fail_out=efail)
        if not targets:
            if efail.get('failed'):
                d, e = efail['failed'][0]
                Tui.err(u"读取库 '%s' 的表列表失败：%s" % (d, e))
                Tui.hint(u'请确认库名存在、账号有权限')
            else:
                Tui.err('库/表模板未展开出任何目标，请检查占位符与匹配')
            Tui.readline(Tui.dim('  回车返回... '))
            return
        self.ctx['targets'] = len(targets)

        # ② 一次填查询条件：字段 / where / LIMIT（+ 可选高级子句）
        self.screen(u'条件')

        # 查询方式：普通（手写条件）或批量（值清单 IN）
        mode = Tui.readline(
            u'  ' + Tui.dim(u'› 查询方式（回车=普通；i=值清单批量 IN）') + u': ')
        if mode == SENTINEL_EOF:
            return
        if mode.strip().lower() in ('i', 'in', 'batch'):
            self._bulk_query_flow(db_name, targets)
            return

        query = self.ask_query_parts(db_name, targets)
        if query is None:
            return

        # 多表 + 聚合：确认可安全合并（不可合并则在此终止并提示缩表）
        merged_note = None
        if len(targets) > 1 and query.has_group:
            try:
                from core.aggregate_merger import analyze as _analyze_agg
                from core.sql_builder import merge_hint as _merge_hint
                _analyze_agg(query)
                merged_note = _merge_hint(query, len(targets)) or (
                    u'将按分组键跨 %d 张表合并为全局聚合' % len(targets))
            except Exception as e:
                Tui.err(u'%s' % e)
                Tui.hint('可改用单表查询（选具体库/表索引）或配 routingField 规则自动缩表')
                Tui.readline(Tui.dim('  回车返回... '))
                return
        elif len(targets) > 1 and not query.has_group:
            # 多表 + 聚合但无 GROUP BY：各表结果相互独立，按表逐行列出（不做全局合计）
            try:
                from core.aggregate_merger import parse_select_columns as _parse_cols
                _has_agg = any(c.is_agg for c in _parse_cols(query.fields))
            except Exception:
                _has_agg = False
            if _has_agg:
                merged_note = (u'提示：多表聚合但未分组，将按表逐行列出（共 %d 行）；'
                               u'需要全局合计请加 GROUP BY 分组键' % len(targets))

        # ③ 智能缩表（并入预览确认，不再单独打断）
        shrink_meta = None
        if len(targets) > 1 and query.where != '':
            targets, shrink_meta = self.suggest_shrink(targets, query.where)
            self.ctx['targets'] = len(targets)
            self.ctx['shrink'] = shrink_meta or ''

        # ④ SQL 预览即确认（回车执行）
        self.screen(u'预览')
        if not self.preview_and_confirm(db_name, targets, query, shrink_meta, merged_note):
            return

        # ⑤ 执行
        self.screen(u'执行')
        quiet = (len(targets) == 1)
        out = self.executor.execute_guided_targets(
            targets, query, db_name, quiet=quiet)
        stat = out['statistics']
        parts = [u'成功 %d' % stat['success']]
        if stat['failed']:
            parts.append(Tui.yellow(u'失败 %d' % stat['failed']))
        parts.append(u'耗时 %.2fs' % stat['total_exec_time'])
        Tui.ok(u'执行完成  ·  ' + u'  ·  '.join(parts))
        if stat['errors']:
            for e in stat['errors'][:20]:
                Tui._out('    ' + Tui.yellow(u'! %s: %s' % (e['target'], e['error'])) + u'\n')

        all_data = self.aggregator.collect(out['results'])
        # 多表 + GROUP BY：客户端二次合并为全局聚合
        if len(targets) > 1 and query.has_group:
            from core.aggregate_merger import merge as _merge_agg
            all_data = _merge_agg(all_data, query)
            self.ctx['result'] = u'上次结果  %d 行（已跨 %d 张表合并）' % (len(all_data), len(targets))
        else:
            self.ctx['result'] = u'上次结果  %d 行（%s）' % (
                len(all_data), self.ctx['table_template'] or self.ctx['db_template'])
        self.show_result_actions(None, all_data, merged=(len(targets) > 1 and query.has_group))

    # ------------------------------------------------------------------
    # 批量值清单查询（值文件 + 字段 -> WHERE 字段 IN (...) 分批）
    # ------------------------------------------------------------------
    def _pick_value_file(self):
        """
        选择一个值清单文件。

        优先扫描基准数据目录（默认 <root>/data）并列成菜单；
        菜单里始终保留「手动输入路径」；无文件时直接引导手动输入。
        返回解析后的绝对路径；取消返回 ''。
        """
        from core import data_dir as _dd

        base = _dd.get_data_dir(self.rootDir, self.dataDir)
        if not os.path.isdir(base):
            try:
                _dd.ensure_data_dir(self.rootDir, self.dataDir)
                Tui.hint(u'已创建基准目录 %s，可把值清单文件放进去' % base)
            except (IOError, OSError) as e:
                Tui.warn(u'基准目录不可用（%s），请手动输入文件路径' % e)

        files = _dd.scan(base)
        Tui._out(u'\n  ' + Tui.bold(u'值清单文件') + u'\n')
        Tui._out(u'  ' + Tui.dim(u'基准目录  %s' % base) + u'\n')

        if not files:
            Tui.hint(u'该目录下暂无可用文件（.csv/.txt/.tsv/.list/.dat）')
            return self._ask_value_file_path(base)

        items = [u'手动输入路径'] + list(files)
        choice = Tui.menu(u'', items, True, 20)
        if choice == -1:
            return ''
        if choice == 0:
            return self._ask_value_file_path(base)

        picked = os.path.join(base, files[choice - 1])
        Tui.ok(u'已选择  %s' % files[choice - 1])
        return picked

    def _ask_value_file_path(self, base):
        """手动输入值文件路径（纯文件名按基准目录解析）。返回绝对路径；取消 ''。"""
        from core import data_dir as _dd

        rel = Tui.ask('值文件路径（可直接写文件名，或完整路径）', '')
        if rel == SENTINEL_EOF:
            return ''
        rel = normalize_chars(rel.strip())
        if rel == '':
            Tui.err('未提供值文件路径')
            return ''
        return _dd.resolve(rel, self.rootDir, self.dataDir)

    def _bulk_query_flow(self, db_name, targets):
        """值清单批量查询：读值文件 -> 指定字段 -> 预览 -> 分批执行 -> 结果。"""
        from core.value_list_loader import ValueListLoader
        from core.in_batcher import InBatcher

        # ① 值文件（默认在基准数据目录里选，也可手输路径）
        path = self._pick_value_file()
        if path == '':
            return

        # ② 字段名
        field = Tui.ask('字段名（如 F_log_id）', '')
        if field == SENTINEL_EOF:
            return
        field = normalize_chars(field.strip())
        if field == '':
            Tui.err('字段名必填')
            return

        # ③ 加载值清单（表头含同名字段时自动定位该列）
        loader = ValueListLoader()
        try:
            values, meta = loader.load(path, has_header=None, field=field)
        except DbrError as e:
            Tui.err(u'%s' % e)
            Tui.hint('可用 --no-header / --has-header 指定表头策略')
            return
        except Exception as e:
            Tui.err(u'读取值文件失败: %s' % e)
            return

        head_note = '（已识别表头并跳过首行）' if meta['header_dropped'] else ''
        col_note = u''
        if meta.get('column_name'):
            col_note = u' · 取列「%s」' % meta['column_name']
        elif meta.get('column'):
            col_note = u' · 取第 %d 列' % meta['column']
        Tui.ok(u'已加载 %d 个去重值（原始 %d 行%s）%s%s' % (
            len(values), meta['total_lines'],
            '，跳过空行 %d' % meta['skipped_empty'] if meta['skipped_empty'] else '',
            head_note, col_note))

        # ④ 其余查询子句（字段 / LIMIT / 高级子句）；where 由 IN 清单生成
        Tui.hint('接下来设置 SELECT 字段 / LIMIT / 排序等；where 由值清单自动生成')
        base = self.ask_query_parts(db_name, targets)
        if base is None:
            return

        batcher = InBatcher(DEFAULT_BULK_BATCH_SIZE)
        try:
            where_list = batcher.build_in_where(field, values)
        except DbrError as e:
            Tui.err(u'%s' % e)
            return

        # 确保字段在 SELECT 中，便于统计命中值
        base.fields = _ensure_field_in_fields(base.fields, field)

        n_tables = len(targets)
        n_batches = len(where_list)
        total_q = n_tables * n_batches

        # ⑤ 预览
        self.screen(u'预览')
        Tui._out('\n')
        if not Tui.clear_enabled():
            scope = '占位符遍历' if n_tables > 1 else '单表'
            Tui.keyvals([('目标', '%d 张表  ·  %s' % (n_tables, scope))])
        Tui.ok(u'值清单批量查询  ·  %d 个值  ·  %d 批（每批 ≤%d）' % (
            len(values), n_batches, batcher.batch_size))
        Tui._out(u'  ' + Tui.dim(u'将对每张表执行以下 WHERE（分批）：') + u'\n')
        Tui._out(u'      ' + batcher.preview_where(field, values) + u'\n')
        Tui._out(u'  ' + Tui.dim(
            u'共将执行 %d 次查询（表 %d × 批 %d）' % (total_q, n_tables, n_batches)) + u'\n')
        if total_q > BULK_CONFIRM_THRESHOLD:
            Tui.warn(u'查询次数较多（%d 次），耗时可能较长' % total_q)
        Tui._out('\n')
        ans = Tui.readline('  ' + Tui.dim('› 回车执行 · n 取消') + ': ')
        if ans == SENTINEL_EOF or ans.strip().lower() in ('n', 'no'):
            return

        # ⑥ 执行
        self.screen(u'执行')
        quiet = (len(targets) == 1 and n_batches == 1)
        out = self.executor.execute_guided_targets(
            targets, base, db_name, quiet=quiet, where_list=where_list)
        stat = out['statistics']
        parts = [u'成功 %d' % stat['success']]
        if stat['failed']:
            parts.append(Tui.yellow(u'失败 %d' % stat['failed']))
        parts.append(u'耗时 %.2fs' % stat['total_exec_time'])
        Tui.ok(u'批量执行完成  ·  ' + u'  ·  '.join(parts))
        if stat['errors']:
            for e in stat['errors'][:20]:
                Tui._out('    ' + Tui.yellow(u'! %s: %s' % (e['target'], e['error'])) + u'\n')

        all_data = self.aggregator.collect(out['results'])

        # ⑦ 命中统计（按字段值匹配）
        hit, miss = _bulk_hit_stats(all_data, field, values)
        summary = u'输入 %d 值' % len(values)
        if hit is not None:
            summary += u'  ·  命中 %d  ·  未命中 %d' % (hit, len(values) - hit)
        summary += u'  ·  结果 %d 行' % len(all_data)
        if stat['failed']:
            summary += u'  ·  失败批 %d' % stat['failed']
        self.ctx['result'] = u'上次结果  ' + summary

        self.show_result_actions(None, all_data, merged=False)

    # ------------------------------------------------------------------
    # SHOW DATABASES / TABLES
    # ------------------------------------------------------------------
    def show_databases(self, conn, err_out=None):
        """读取库列表。失败时返回 []，并把真实错误写入 err_out（若提供）。"""
        try:
            with Tui.spinner(u'正在读取库列表…'):
                names = self.sql.query_scalar_list(conn, 'SHOW DATABASES')
        except Exception as e:
            if err_out is not None:
                err_out['error'] = u'%s' % e
            return []
        skip = ('information_schema', 'performance_schema', 'mysql', 'sys')
        return [n for n in names if n not in skip]

    def show_tables(self, conn, db, err_out=None):
        """
        读取指定库的表列表。
        失败时返回 []，并把真实错误写入 err_out（若提供）——用于区分
        「库确实为空」与「SHOW TABLES 执行失败（权限/超时/库不存在）」。

        用 `-D <db>` 连接后执行 `SHOW TABLES`（而非 `SHOW TABLES FROM`）：
        部分 MySQL 分支（如 5.0 DTM）要求连接必须先选库，否则 SHOW TABLES FROM
        会报 "ERROR 5042 no database"。
        """
        safe = str(db).replace('`', '')
        try:
            return self.sql.query_scalar_list(
                conn, 'SHOW TABLES', database=safe)
        except Exception as e:
            if err_out is not None:
                err_out['error'] = u'%s' % e
            return []

    def fetch_tables_for_dbs(self, conn, db_list, fail_out=None):
        """
        一次取齐多个库的表清单（替代逐库 SHOW TABLES，显著加速多库展开）。

        优先用 information_schema.tables 一条（分批）SQL 拿齐；
        若查询失败或某些库缺失，则对相应库回退逐库 SHOW TABLES，
        保证可用性与「库读取失败」提示语义不退化。

        返回 {db: [table, ...]}（只含 key=查询过的库）。
        """
        wanted = [d for d in db_list if d]
        if not wanted:
            return {}

        result = {}
        missing = list(wanted)

        rows = self._query_information_schema_tables(conn, wanted)
        if rows is not None:
            for schema, table in rows:
                result.setdefault(schema, []).append(table)
            # 某些库在 information_schema 里查不到（无权限/不存在）：
            # 对缺失库回退逐库 SHOW TABLES，既拿真实结果也拿真实错误。
            missing = [d for d in wanted if d not in result]

        # 回退：查询失败（rows is None -> 全部库）或个别库缺失
        for db in missing:
            terr = {}
            tbls = self.show_tables(conn, db, err_out=terr)
            if terr.get('error') and fail_out is not None:
                fail_out.setdefault('failed', []).append((db, terr['error']))
            result[db] = tbls

        return result

    # 一次性查询 information_schema 时每个 IN 批次的库数上限
    _IS_DB_BATCH = 200

    def _query_information_schema_tables(self, conn, dbs):
        """
        用 information_schema.tables 批量取库表，返回 [(schema, table), ...]；
        失败返回 None（调用方回退逐库 SHOW TABLES）。

        兼容「要求连接必须先选库」的 MySQL 分支：带 -D <某库> 连接。
        """
        out = []
        batch = max(1, self._IS_DB_BATCH)
        # 选一个已存在的库作为连接默认库（避免 5042 no database）
        anchor = dbs[0] if dbs else None
        for i in range(0, len(dbs), batch):
            chunk = dbs[i:i + batch]
            in_list = ','.join("'%s'" % _escape_sql_literal(d) for d in chunk)
            sql = ("SELECT TABLE_SCHEMA, TABLE_NAME FROM information_schema.tables "
                   "WHERE TABLE_SCHEMA IN (%s)" % in_list)
            try:
                rows = self.sql.query(conn, sql, database=anchor)
            except Exception:
                return None
            for r in rows:
                schema = _first_col(r, 'TABLE_SCHEMA')
                table = _first_col(r, 'TABLE_NAME')
                if schema is not None and table is not None:
                    out.append((schema, table))
        return out

    # ------------------------------------------------------------------
    # 表结构（条件屏输 `?` 查看）
    # ------------------------------------------------------------------
    # information_schema.columns 查询时每个 IN 批次的表数上限
    _IS_TABLE_BATCH = 200

    def fetch_table_structure(self, conn, db, tables):
        """
        取指定库里若干表的字段结构。

        返回 {table: [(field, type, nullable, key, default, comment), ...]}

        优先用 information_schema.columns 一条（分批）SQL 取齐；
        失败时回退逐表 SHOW FULL COLUMNS。查询带 -D <db>，兼容要求先选库的分支。
        """
        wanted = [t for t in (tables or []) if t]
        if not wanted:
            return {}

        result = self._query_information_schema_columns(conn, db, wanted)
        if result is not None:
            return result

        # 回退：逐表 SHOW FULL COLUMNS
        result = {}
        for t in wanted:
            result[t] = self._show_full_columns(conn, db, t)
        return result

    def _query_information_schema_columns(self, conn, db, tables):
        """information_schema.columns 批量取字段；查询失败返回 None。"""
        out = {}
        batch = max(1, self._IS_TABLE_BATCH)
        for i in range(0, len(tables), batch):
            chunk = tables[i:i + batch]
            in_list = ','.join("'%s'" % _escape_sql_literal(t) for t in chunk)
            sql = ("SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, "
                   "COLUMN_KEY, COLUMN_DEFAULT, COLUMN_COMMENT "
                   "FROM information_schema.columns "
                   "WHERE TABLE_SCHEMA='%s' AND TABLE_NAME IN (%s) "
                   "ORDER BY TABLE_NAME, ORDINAL_POSITION"
                   % (_escape_sql_literal(db), in_list))
            try:
                rows = self.sql.query(conn, sql, database=db)
            except Exception:
                return None
            for r in rows:
                t = _first_col(r, 'TABLE_NAME')
                if t is None:
                    continue
                out.setdefault(t, []).append((
                    _first_col(r, 'COLUMN_NAME'),
                    _first_col(r, 'COLUMN_TYPE'),
                    _first_col(r, 'IS_NULLABLE'),
                    _first_col(r, 'COLUMN_KEY'),
                    _first_col(r, 'COLUMN_DEFAULT'),
                    _first_col(r, 'COLUMN_COMMENT'),
                ))
        return out

    def _show_full_columns(self, conn, db, table):
        """回退方案：SHOW FULL COLUMNS FROM db.table -> 同结构元组列表。"""
        sql = "SHOW FULL COLUMNS FROM `%s`.`%s`" % (
            str(db).replace('`', ''), str(table).replace('`', ''))
        try:
            rows = self.sql.query(conn, sql, database=db)
        except Exception:
            return []
        out = []
        for r in rows:
            out.append((
                _first_col(r, 'Field'),
                _first_col(r, 'Type'),
                _first_col(r, 'Null'),
                _first_col(r, 'Key'),
                _first_col(r, 'Default'),
                _first_col(r, 'Comment'),
            ))
        return out

    def show_table_structure(self, conn, targets):
        """
        展示所选表的字段结构（供条件屏输 `?` 调用）。

        分片表结构一致时只展示一次并注明；不一致时逐表分段展示。
        就地打印（不清屏），展示后由调用方重新提示原问题。
        """
        if not targets:
            return
        db = targets[0].get('dbName') or u''
        # 只取「同一个库」下的表：分片表结构一致，用第一个库作代表即可；
        # （库模板遍历时 targets 会跨多个库，不能把其它库的表名混进来）
        tables = []
        for t in targets:
            if (t.get('dbName') or u'') != db:
                continue
            tn = t.get('tableName')
            if tn and tn not in tables:
                tables.append(tn)
        if not tables:
            return

        Tui._out(u'\n  ' + Tui.bold(u'表结构') + u'  ' +
                 Tui.dim(u'%s  ·  %d 张表' % (db, len(tables))) + u'\n')
        Tui.rule()

        try:
            struct = self.fetch_table_structure(conn, db, tables)
        except Exception as e:
            Tui.err(u'读取表结构失败：%s' % e)
            return

        # 按结构签名分组：同结构的分表归为一组
        groups = []          # [[signature, [tables], rows], ...]
        for t in tables:
            rows = struct.get(t) or []
            if not rows:
                continue
            sig = _struct_signature(rows)
            for g in groups:
                if g[0] == sig:
                    g[1].append(t)
                    break
            else:
                groups.append([sig, [t], rows])

        missing = [t for t in tables if not (struct.get(t) or [])]
        if missing:
            Tui.warn(u'未取到结构的表：%s' % ', '.join(missing[:5]))
        if not groups:
            Tui.err(u'未取到任何表结构（表可能不存在或无权限）')
            return

        for sig, tbls, rows in groups:
            if len(tbls) > 1:
                Tui._out(u'  ' + Tui.dim(u'%d 张表结构一致，取自 %s.%s' % (
                    len(tbls), db, tbls[0])) + u'\n')
            else:
                Tui._out(u'  ' + Tui.dim(u'取自 %s.%s' % (db, tbls[0])) + u'\n')
            disp = [_struct_display_row(r) for r in rows]
            self.renderer.render(list(_STRUCT_HEADERS), disp)

        Tui._out(u'  ' + Tui.dim(u'提示：可复制字段名用于 SELECT 字段 / where 条件') + u'\n')

    def _ask_or_structure(self, prompt, default, conn, targets):
        """
        读取一行输入；输入 `?`（含全角 ？）时先展示表结构，再重新提示同一问题。
        conn/targets 为空（未提供上下文）时退化为普通输入。
        """
        while True:
            val = Tui.ask(prompt, default)
            if val == SENTINEL_EOF:
                return val
            if conn and targets and normalize_chars(val.strip()) == u'?':
                self.show_table_structure(conn, targets)
                Tui._out(u'\n')
                continue
            return val

    # ------------------------------------------------------------------
    # 分组识别
    # ------------------------------------------------------------------
    @staticmethod
    def group_databases(dbs):
        groups = {}
        singles = []
        for db in dbs:
            m = re.match(r'^(.*?)(\d+)$', db)
            if m and m.group(1) != '':
                prefix = m.group(1)
                groups.setdefault(prefix, []).append(db)
            else:
                singles.append(db)
        items = []
        for prefix, lst in groups.items():
            if len(lst) >= 2:
                items.append({'kind': 'group', 'prefix': prefix, 'count': len(lst), 'dbs': lst})
            else:
                singles.append(lst[0])
        for s in singles:
            items.append({'kind': 'single', 'name': s})
        return items

    @staticmethod
    def group_tables(tables):
        groups = {}
        singles = []
        for t in tables:
            m = re.match(r'^(.*?)(\d+_\d+)$', t)
            if m and m.group(1) != '':
                prefix = m.group(1)
                groups.setdefault(prefix, []).append(t)
            else:
                singles.append(t)
        items = []
        for prefix, lst in groups.items():
            if len(lst) >= 2:
                items.append({'kind': 'group', 'prefix': prefix, 'count': len(lst), 'tables': lst})
            else:
                singles.append(lst[0])
        for s in singles:
            items.append({'kind': 'single', 'name': s})
        return items

    # ------------------------------------------------------------------
    # 模板 -> 正则
    # ------------------------------------------------------------------
    @staticmethod
    def template_to_regex(template):
        quoted = re.escape(template)
        # 占位符只匹配「纯数字」分片索引：
        #   - 不含下划线：避免 {db} 跨段贪婪，如 t_order_sign_{db}_4 误吃 t_order_log_78_4；
        #   - 且限定为数字：避免把表名中的单词当索引，
        #     如 t_order_{db}_{table} 误命中 t_order_lock_202701（{db}=lock）。
        for ph in ('\\{db\\}', '\\{table\\}', '\\{dbIndex\\}', '\\{tableIndex\\}'):
            quoted = quoted.replace(ph, r'([0-9]+)')
        return '^' + quoted + '$'

    @staticmethod
    def first_matching_db(template, dbs):
        if '{' not in template:
            return template if template in dbs else None
        pattern = Repl.template_to_regex(template)
        for db in dbs:
            if re.match(pattern, db):
                return db
        return None

    # ------------------------------------------------------------------
    # ① A 选库（一步菜单，行内缩范围；整组遍历在“选到库组模板”时生效）
    # ------------------------------------------------------------------
    def pick_database(self, conn, dbs_out):
        """返回 db_template（库名或 `前缀{db}` 模板），取消返回 ''。

        列表以「逻辑库组」为主；单库默认折叠进「更多…」，避免一长串平铺。
        """
        derr = {}
        dbs = self.show_databases(conn, err_out=derr)
        dbs_out[:] = dbs
        if not dbs:
            if derr.get('error'):
                Tui.err(u'读取库列表失败：%s' % derr['error'])
            else:
                Tui.err('该数据源没有可见数据库')
            return ''
        groups = self.group_databases(dbs)
        db_groups = [g for g in groups if g['kind'] == 'group']
        singles = [g['name'] for g in groups if g['kind'] == 'single']

        while True:
            items = [u'手动输入库名/模板']
            meta = [None]
            for g in db_groups:
                items.append(g['prefix'] + u'{db}')
                meta.append(g)
            if singles:
                items.append(u'更多数据库…')
                meta.append({'kind': 'more'})

            Tui.breadcrumb(conn, '选库')
            Tui._cmd_hint(len(db_groups), len(singles))
            choice, raw = Tui.filter_menu('', items, True, 20)
            if choice == -1:
                return ''
            if choice == -2:
                return raw.strip()
            if choice == 0:
                t = Tui.ask('库名或库模板', '')
                return t.strip() if t != SENTINEL_EOF else ''

            g = meta[choice]
            if g is not None and g.get('kind') == 'more':
                picked = self._pick_single_db(singles)
                if picked == '':
                    continue
                return picked
            if g['kind'] == 'single':
                return g['name']

            # 逻辑库组：行内一问是否缩到具体库（回车=整组遍历）
            extra = Tui.ask('缩到具体库索引（回车=遍历全部 %d 库）' % g['count'], '')
            if extra != SENTINEL_EOF and extra.strip():
                return g['prefix'] + extra.strip().split()[0]
            return g['prefix'] + u'{db}'

    def _pick_single_db(self, singles):
        """从单库列表里选一个（或手输）。返回库名或 ''。"""
        items = [u'手动输入'] + list(singles)
        choice, raw = Tui.filter_menu('', items, True, 20)
        if choice == -1:
            return ''
        if choice == -2:
            return raw.strip()
        if choice > 0:
            return singles[choice - 1]
        t = Tui.ask('库名或模板', '')
        return t.strip() if t != SENTINEL_EOF else ''

    # ------------------------------------------------------------------
    # ① B 选表（一步菜单，行内缩范围；整组遍历在“选到表组模板”时生效）
    # ------------------------------------------------------------------
    def pick_table(self, conn, base_db, tables_out):
        """返回 table_template（表名或 `前缀{db}_{table}` 模板），取消返回 ''。"""
        terr = {}
        tables = self.show_tables(conn, base_db, err_out=terr)
        tables_out[:] = tables
        if not tables:
            if terr.get('error'):
                # 执行失败（权限/超时/库不存在等）：透出真实错误，避免伪装成"没有表"
                Tui.err(u"读取库 '%s' 的表列表失败：%s" % (base_db, terr['error']))
                Tui.hint(u'请确认库名存在、账号有权限，或该库确实为空')
            else:
                Tui.err(u"库 '%s' 没有可见表" % base_db)
            return ''
        groups = self.group_tables(tables)
        tbl_groups = [g for g in groups if g['kind'] == 'group']
        singles = [g['name'] for g in groups if g['kind'] == 'single']

        while True:
            items = [u'手动输入表名/模板']
            meta = [None]
            for g in tbl_groups:
                items.append(g['prefix'] + u'{db}_{table}')
                meta.append(g)
            if singles:
                items.append(u'更多表…')
                meta.append({'kind': 'more'})

            Tui.breadcrumb(conn, base_db, '选表')
            Tui._cmd_hint(len(tbl_groups), len(singles))
            choice, raw = Tui.filter_menu('', items, True, 20)
            if choice == -1:
                return ''
            if choice == -2:
                return raw.strip()
            if choice == 0:
                t = Tui.ask('表名或表模板', '')
                return t.strip() if t != SENTINEL_EOF else ''

            g = meta[choice]
            if g is not None and g.get('kind') == 'more':
                picked = self._pick_single_table(singles)
                if picked == '':
                    continue
                return picked
            if g['kind'] == 'single':
                return g['name']

            # 逻辑表组：行内一问是否缩到具体表（回车=整组遍历）
            extra = Tui.ask('缩到具体表索引（回车=遍历全部 %d 张/库）' % g['count'], '')
            if extra != SENTINEL_EOF and extra.strip():
                return g['prefix'] + u'{db}_' + extra.strip().split()[0]
            return g['prefix'] + u'{db}_{table}'

    def _pick_single_table(self, singles):
        items = [u'手动输入'] + list(singles)
        choice, raw = Tui.filter_menu('', items, True, 20)
        if choice == -1:
            return ''
        if choice == -2:
            return raw.strip()
        if choice > 0:
            return singles[choice - 1]
        t = Tui.ask('表名或模板', '')
        return t.strip() if t != SENTINEL_EOF else ''

    # ------------------------------------------------------------------
    # ② 分步填查询条件：字段 → where → LIMIT →（可选）高级子句
    # ------------------------------------------------------------------
    def ask_query_parts(self, conn=None, targets=None):
        """
        分步输入，各自独立提示、回车用默认：
          1) SELECT 字段   默认 *（可输 ? 查看表结构）
          2) where 条件    默认为空（不加 where；可输 ? 查看表结构）
          3) LIMIT 行数    默认 100（输 NONE 不限制）
          4) 高级子句      回车跳过；输 o/g/h 可编辑 排序/分组/HAVING
        返回 QueryParts；取消返回 None。

        conn/targets 提供时，「字段 / where」两处支持输 ? 就地查看所选表结构。
        """
        # 表结构提示（仅在能取到结构上下文时显示）
        if conn and targets:
            Tui.hint(u'表结构：输入 ? 可查看所选表字段（类型 / 键 / 注释）')

        # ① 字段
        fields = self._ask_or_structure('SELECT 字段', '*', conn, targets)
        if fields == SENTINEL_EOF:
            return None
        fields = normalize_chars(fields.strip()) or '*'

        # ② where
        where = self._ask_or_structure('where 条件（回车=不加）', '', conn, targets)
        if where == SENTINEL_EOF:
            return None
        where = normalize_chars(where.strip())

        # ③ LIMIT
        limit = Tui.ask('LIMIT 行数（NONE=不限制）', '100')
        if limit == SENTINEL_EOF:
            return None
        limit = normalize_chars(limit.strip()) or '100'
        if str(limit).upper() != 'NONE' and not str(limit).isdigit():
            Tui.warn('LIMIT 非数字，已按 100 处理')
            limit = '100'

        query = QueryParts(fields=fields, where=where, limit=limit)

        # ④ 高级子句（可选）：ORDER BY / GROUP BY / HAVING
        self._ask_advanced_clauses(query)
        return query

    def _ask_advanced_clauses(self, query):
        """
        可选的高级子句编辑：在提示行输 o/g/h 进入对应编辑，
        直接回车跳过；编辑后可继续输入或回车结束。
        """
        while True:
            ans = Tui.readline(
                u'  ' + Tui.dim(u'› 高级子句（回车=跳过；o 排序 / g 分组 / h HAVING）') + u': ')
            if ans == SENTINEL_EOF:
                return
            key = ans.strip().lower()
            if key == '':
                return
            if key == 'o':
                v = Tui.ask('ORDER BY（如 F_id 或 F_id DESC, F_time）', query.order_by)
                if v == SENTINEL_EOF:
                    return
                query.order_by = normalize_chars(v.strip())
            elif key == 'g':
                v = Tui.ask('GROUP BY（逗号分隔，如 F_a, F_b）', query.group_by)
                if v == SENTINEL_EOF:
                    return
                query.group_by = normalize_chars(v.strip())
            elif key == 'h':
                v = Tui.ask('HAVING（如 COUNT(*) > 1）', query.having)
                if v == SENTINEL_EOF:
                    return
                query.having = normalize_chars(v.strip())
            else:
                Tui.warn('可选 o 排序 / g 分组 / h HAVING，回车跳过')

    @staticmethod
    def _parse_query_parts(line):
        """
        把一行查询条件解析为 QueryParts。

        支持两种写法：
          旧：字段 | where | LIMIT
          新：字段 | where | LIMIT | order by F_id DESC | group by F_a | having COUNT(*)>1
        （order by / group by / having 段可任意顺序、可缺省）
        """
        line = (line or '').strip()
        if line == '':
            return QueryParts()

        parts = None
        for sep in ('|', ';;', '||'):
            if sep in line:
                parts = [p.strip() for p in line.split(sep)]
                break
        if parts is None:
            low = line.lower()
            if '=' in line or ' like ' in low or ' in ' in low:
                return QueryParts(where=line)
            if line.isdigit() or low == 'none':
                return QueryParts(limit=line)
            return QueryParts(fields=line)

        fields, where, limit = u'*', u'', u'100'
        order_by, group_by, having = u'', u'', u''
        positional = 0
        for seg in parts:
            low = seg.lower()
            if low.startswith('order by'):
                order_by = seg[len('order by'):].strip()
            elif low.startswith('group by'):
                group_by = seg[len('group by'):].strip()
            elif low.startswith('having'):
                having = seg[len('having'):].strip()
            elif seg.isdigit() or low == 'none':
                # 纯数字/none 段一律视为 LIMIT（where 不可能是纯数字）
                limit = seg or u'100'
            else:
                if positional == 0:
                    fields = seg or u'*'
                elif positional == 1:
                    where = seg
                else:
                    limit = seg or u'100'
                positional += 1
        return QueryParts(fields=fields, where=where, limit=limit,
                          order_by=order_by, group_by=group_by, having=having)

    # ------------------------------------------------------------------
    # ③ 智能缩表（并入预览，不再单独打断）
    # ------------------------------------------------------------------
    def suggest_shrink(self, targets, where):
        """
        若 where 命中某规则的路由字段等值条件，返回 (缩表后的单个 target, 提示信息)；
        否则原样返回 (targets, None)。不再单独弹出确认，交由预览确认统一处理。
        """
        for rn in self.loader.get_rule_names():
            ru = self.loader.get_rule(rn)
            if not self.route.can_route_field(ru):
                continue
            rf = self.route.get_routing_field(ru)
            m = re.search(r'\b%s\s*=\s*[\'"]([^\'"]+)[\'"]' % re.escape(rf), where)
            if m:
                val = m.group(1)
                try:
                    tg = self.route.compute_target(val, ru)
                    new_target = [{
                        'dbName': tg['dbName'],
                        'tableName': tg['tableName'],
                        'dbIndex': tg['dbIndex'],
                        'tableIndex': tg['tableIndex'],
                        'date': '',
                    }]
                    note = '✔ 已按路由字段 %s=%s 缩表，仅查 1 张表：%s.%s' % (
                        rf, val, tg['dbName'], tg['tableName'])
                    return new_target, note
                except Exception:
                    pass
                return targets, None
        return targets, None

    # ------------------------------------------------------------------
    # ④ SQL 预览即确认（回车执行）
    # ------------------------------------------------------------------
    def preview_and_confirm(self, conn, targets, query, shrink_meta, merged_note=None):
        """展示目标与 SQL 预览，回车即确认执行。返回 True 表示继续执行。"""
        Tui._out('\n')
        n = len(targets)
        scope = '占位符遍历' if n > 1 else '单表'
        # 清屏开启时，目标已由顶部摘要行展示，这里不再重复
        if not Tui.clear_enabled():
            Tui.keyvals([('目标', '%d 张表  ·  %s' % (n, scope))])
        if n > 200:
            Tui.warn('目标较多，遍历耗时较长；可缩小库/表模板或配 routingField 规则')
        if shrink_meta:
            Tui.ok(shrink_meta)
        if merged_note:
            Tui.ok(merged_note)

        Tui._out('\n')
        # 多表 + GROUP BY + HAVING：展示真实下发到每张表的 SQL（HAVING 由合并后判定）
        preview_parts = query
        if n > 1 and query.has_group and query.has_having:
            preview_parts = QueryParts(fields=query.fields, where=query.where,
                                       group_by=query.group_by, having=u'',
                                       order_by=u'', limit=query.limit)
            Tui.hint('（HAVING 在跨表合并后统一判定，故不出现于单表 SQL）')
        self._print_sql_preview(preview_parts, targets)
        Tui._out('\n')

        ans = Tui.readline('  ' + Tui.dim('› 回车执行 · n 取消') + ': ')
        if ans == SENTINEL_EOF:
            return False
        if ans.strip().lower() in ('n', 'no'):
            return False
        return True

    def _print_sql_preview(self, parts, targets, shown=3):
        """
        预览将要执行的 SQL，保证信息不丢：
          * 公共前缀 `SELECT <fields> FROM ` 只打印一次（灰）；
          * 每条 SQL 的「库.表 + 子句」完整展示（表名绝不截断）；
          * 子句含 {db}/{table} 占位符时逐表渲染（各表子句不同），否则共用后缀。
        """
        term = Tui.term_width()
        prefix = u'SELECT %s FROM ' % parts.fields
        single = len(targets) <= 1

        if single:
            # 单表：一行完整展示，不拆公共前缀
            t = targets[0]
            full = build_sql(parts, t['dbName'], t['tableName'],
                             t['dbIndex'], t['tableIndex'])
            room = term - 4
            if TableRenderer.display_width(full) > room:
                full = TableRenderer.truncate_middle(full, room)
            Tui._out(u'  ' + full + u'\n')
            return

        if TableRenderer.display_width(prefix) > term - 2:
            prefix = TableRenderer.truncate_middle(prefix, term - 2)
        Tui._out(u'  ' + Tui.dim(prefix) + u'\n')

        # 子句是否含占位符：含则每表不同，必须逐表渲染
        clause_text = u' '.join([parts.where, parts.group_by, parts.having, parts.order_by])
        has_ph = any(p in clause_text for p in (u'{db}', u'{table}', u'{dbIndex}', u'{tableIndex}'))

        for t in targets[:shown]:
            dbtb = u'%s.%s' % (t['dbName'], t['tableName'])
            room = term - 6
            if has_ph:
                # 逐表渲染真实 SQL，去掉公共前缀后展示剩余部分
                full = build_sql(parts, t['dbName'], t['tableName'],
                                 t['dbIndex'], t['tableIndex'])
                line = full[len(prefix):] if full.startswith(prefix) else full
            else:
                suffix = build_sql(parts, u'X', u'X').split(u'.X', 1)[-1]
                line = dbtb + suffix
            if TableRenderer.display_width(line) > room:
                # 库表名的区分位在尾部（_78_0 / _02），优先保留尾部；
                # 若能放下「库.表」，则只截子句、完整保留库.表。
                tb_w = TableRenderer.display_width(dbtb)
                if tb_w <= room:
                    clause = line[tb_w:]
                    keep = room - tb_w
                    line = dbtb + TableRenderer.truncate(clause, keep)
                else:
                    line = TableRenderer.truncate_tail(line, room)
            Tui._out(u'      ' + line + u'\n')
        if len(targets) > shown:
            Tui._out(u'  ' + Tui.dim(u'      … 其余 %d 张' % (len(targets) - shown)) + u'\n')

    # ------------------------------------------------------------------
    # 一次性查询（--query，非交互）
    # ------------------------------------------------------------------
    def run_query_once(self, db_name, spec):
        """
        非交互一次性查询。spec 形如：
          '库.表'                         -> 全默认（select *，limit 100）
          '库.表 字段 | where | LIMIT'     -> 指定
        结果直接打印表格（不分页、不导出）。
        返回 True 成功。
        """
        if not self.db.ensure_connected(db_name):
            return False
        spec = (spec or '').strip()
        if not spec:
            Tui.err('--query 为空')
            return False

        head = spec
        rest = ''
        for sep in ('  ', '\t'):
            if sep in spec:
                head, rest = spec.split(sep, 1)
                break
        if not rest and ' ' in spec:
            head, rest = spec.split(' ', 1)

        derr = {}
        dbs = self.show_databases(db_name, err_out=derr)
        if not dbs and derr.get('error'):
            Tui.err(u'读取库列表失败：%s' % derr['error'])
            return False
        # 拆分 `库.表`
        if '.' in head:
            db_tpl, tbl_tpl = head.split('.', 1)
        else:
            db_tpl, tbl_tpl = head, '*'
        base_db = self.first_matching_db(db_tpl, dbs)
        terr = {}
        tables = self.show_tables(db_name, base_db, err_out=terr) if base_db else []

        query = QueryParts()
        rest = rest.strip()
        if rest:
            query = self._parse_query_parts(rest)

        targets = self.expand_targets_by_template(
            db_tpl, dbs, tbl_tpl, tables, db_name)
        if not targets:
            if not base_db:
                Tui.err(u"未匹配到库：%s" % db_tpl)
            elif terr.get('error'):
                Tui.err(u"读取库 '%s' 的表列表失败：%s" % (base_db, terr['error']))
            else:
                Tui.err('未展开出任何目标：%s' % head)
            return False
        if len(targets) > 1 and query.where:
            targets, _meta = self.suggest_shrink(targets, query.where)

        merged = (len(targets) > 1 and query.has_group)
        if merged:
            try:
                from core.aggregate_merger import analyze as _analyze_agg
                _analyze_agg(query)
            except Exception as e:
                Tui.err(u'%s' % e)
                return False

        out = self.executor.execute_guided_targets(targets, query, db_name, quiet=True)
        all_data = self.aggregator.collect(out['results'])
        if merged:
            from core.aggregate_merger import merge as _merge_agg
            all_data = _merge_agg(all_data, query)
        headers = [h for h in (list(all_data[0].keys()) if all_data else [])
                   if not _is_helper_col(h)]
        if merged:
            disp_headers, disp_rows, note = headers, all_data, None
        else:
            disp_headers, disp_rows, note = display_view(headers, all_data) if all_data else (headers, all_data, None)
        Tui._out(u'\n  ' + Tui.bold(u'聚合结果' if merged else u'查询结果') + u'  ' +
                 Tui.dim(u'%d 行' % len(all_data)) + u'\n')
        Tui.rule()
        if note:
            Tui._out(u'  ' + Tui.dim(u'来源  %s' % note) + u'\n')
        self.renderer.render(disp_headers, disp_rows)
        return True

    # ------------------------------------------------------------------
    # 展开 targets
    # ------------------------------------------------------------------
    def run_bulk_query_once(self, db_name, spec, file_path, field,
                            batch_size=None, has_header=None, column=None):
        """
        非交互批量值清单查询。
          spec:      同 --query 的目标/字段部分，形如
                     '库.表 [字段 | | LIMIT | order by .. | group by .. | having ..]'
                     （where 段会被忽略，改用值清单生成 IN）
          file_path: 值文件路径。绝对路径原样；含 / 的相对路径相对 rootDir；
                     纯文件名优先到基准数据目录（<root>/data）查找。
          field:     用于 IN 的字段名
          batch_size/has_header/column: 可选
        返回 True 成功。
        """
        from core.value_list_loader import ValueListLoader
        from core.in_batcher import InBatcher
        from core import data_dir as _dd

        if not self.db.ensure_connected(db_name):
            return False

        file_path = _dd.resolve(file_path, self.rootDir, self.dataDir)

        loader = ValueListLoader()
        try:
            col_arg = None
            if column is not None and u'%s' % column != '':
                try:
                    col_arg = int(column)
                except (TypeError, ValueError):
                    col_arg = None
            values, meta = loader.load(file_path, has_header=has_header,
                                       column=col_arg, field=field)
        except DbrError as e:
            sys.stderr.write(u'[错误] %s\n' % e)
            return False

        batcher = InBatcher(batch_size if batch_size else DEFAULT_BULK_BATCH_SIZE)
        try:
            where_list = batcher.build_in_where(field, values)
        except DbrError as e:
            sys.stderr.write(u'[错误] %s\n' % e)
            return False

        # 解析目标（spec 的库.表头段）与查询子句（无 where）
        spec = (spec or '').strip()
        head = spec
        rest = ''
        for sep in ('  ', '\t'):
            if sep in spec:
                head, rest = spec.split(sep, 1)
                break
        if not rest and ' ' in spec:
            head, rest = spec.split(' ', 1)

        derr = {}
        dbs = self.show_databases(db_name, err_out=derr)
        if not dbs and derr.get('error'):
            sys.stderr.write(u'[错误] 读取库列表失败：%s\n' % derr['error'])
            return False
        if '.' in head:
            db_tpl, tbl_tpl = head.split('.', 1)
        else:
            db_tpl, tbl_tpl = head, '*'
        base_db = self.first_matching_db(db_tpl, dbs)
        terr = {}
        tables = self.show_tables(db_name, base_db, err_out=terr) if base_db else []

        query = QueryParts()
        rest = rest.strip()
        if rest:
            query = self._parse_query_parts(rest)
            # 批量模式：where 段交由 IN 清单，忽略解析出的 where
            query.where = u''
        # 确保字段在 SELECT 中，便于命中统计
        query.fields = _ensure_field_in_fields(query.fields, field)

        targets = self.expand_targets_by_template(
            db_tpl, dbs, tbl_tpl, tables, db_name)
        if not targets:
            if not base_db:
                sys.stderr.write(u'[错误] 未匹配到库：%s\n' % db_tpl)
            elif terr.get('error'):
                sys.stderr.write(
                    u"[错误] 读取库 '%s' 的表列表失败：%s\n" % (base_db, terr['error']))
            else:
                sys.stderr.write(u'[错误] 未展开出任何目标：%s\n' % head)
            return False

        n_tables = len(targets)
        n_batches = len(where_list)
        sys.stderr.write(
            u'[信息] 批量值清单：%d 个值 · %d 批（每批 ≤%d）· 目标 %d 张表 · '
            u'共 %d 次查询\n' % (
                len(values), n_batches, batcher.batch_size, n_tables,
                n_tables * n_batches))

        quiet = (n_tables == 1 and n_batches == 1)
        out = self.executor.execute_guided_targets(
            targets, query, db_name, quiet=quiet, where_list=where_list)
        stat = out['statistics']
        all_data = self.aggregator.collect(out['results'])

        hit, _ = _bulk_hit_stats(all_data, field, values)
        sys.stderr.write(u'[信息] 批量完成：成功 %d · 失败 %d · 耗时 %.2fs\n' % (
            stat['success'], stat['failed'], stat['total_exec_time']))
        for e in stat['errors'][:20]:
            sys.stderr.write(u'  ! %s: %s\n' % (e['target'], e['error']))

        headers = [h for h in (list(all_data[0].keys()) if all_data else [])
                   if not _is_helper_col(h)]
        if all_data:
            disp_headers, disp_rows, note = display_view(headers, all_data)
        else:
            disp_headers, disp_rows, note = headers, all_data, None

        summary = u'输入 %d 值' % len(values)
        if hit is not None:
            summary += u'  ·  命中 %d  ·  未命中 %d' % (hit, len(values) - hit)
        summary += u'  ·  结果 %d 行' % len(all_data)
        if stat['failed']:
            summary += u'  ·  失败批 %d' % stat['failed']

        Tui._out(u'\n  ' + Tui.bold(u'批量结果') + u'  ' +
                 Tui.dim(u'%d 行' % len(all_data)) + u'\n')
        Tui.rule()
        Tui._out(u'  ' + Tui.dim(summary) + u'\n')
        if note:
            Tui._out(u'  ' + Tui.dim(u'来源  %s' % note) + u'\n')
        self.renderer.render(disp_headers, disp_rows)
        return True

    # ------------------------------------------------------------------
    # 展开 targets
    # ------------------------------------------------------------------
    def expand_targets_by_template(self, db_template, dbs, table_template, tables, conn,
                                   fail_out=None):
        matched_dbs = self._match_dbs(db_template, dbs)
        if not matched_dbs:
            return []

        matched_tables = {}
        if '{' not in table_template:
            for db in matched_dbs:
                matched_tables[db] = [table_template]
        else:
            pattern = self.template_to_regex(table_template)
            # 一次性取齐多个库的表（information_schema 单条 SQL），避免逐库 SHOW TABLES。
            # 非 TTY 自动静默进度；非 TTY 下 spinner 不渲染。
            total_db = len(matched_dbs)

            def _detail():
                return u'%d 个库' % total_db

            with Tui.spinner(u'正在展开表…', detail_fn=_detail):
                by_db = self.fetch_tables_for_dbs(conn, matched_dbs,
                                                  fail_out=fail_out)
            for db in matched_dbs:
                tbls = by_db.get(db, [])
                matched_tables[db] = [t for t in tbls if re.match(pattern, t)]

        targets = []
        for db in matched_dbs:
            db_index = self.extract_db_index(db)
            for tbl in matched_tables[db]:
                targets.append({
                    'dbName': db,
                    'tableName': tbl,
                    'dbIndex': db_index,
                    'tableIndex': self.extract_table_index(tbl, db_index),
                    'date': '',
                })
        return targets

    @staticmethod
    def _match_dbs(db_template, dbs):
        """按库模板匹配库名（无占位符=精确名；含占位符=正则）。"""
        matched = []
        if '{' not in db_template:
            if db_template in dbs:
                matched.append(db_template)
        else:
            pattern = Repl.template_to_regex(db_template)
            for db in dbs:
                if re.match(pattern, db):
                    matched.append(db)
        return matched

    @staticmethod
    def extract_db_index(db):
        m = re.search(r'(\d+)$', db)
        return m.group(1) if m else ''

    @staticmethod
    def extract_table_index(table, db_index):
        if db_index != '':
            m = re.search(r'_' + re.escape(db_index) + r'_(\d+)$', table)
            if m:
                return m.group(1)
        m = re.search(r'(\d+)$', table)
        return m.group(1) if m else ''

    # ------------------------------------------------------------------
    # 结果展示与操作（单字符快捷指令，不弹菜单）
    # ------------------------------------------------------------------
    def show_result_actions(self, headers, rows, title=None, merged=False):
        if not rows:
            Tui.ok('查询完成，无数据')
            return
        # 全量列表：剥离内部辅助列（__sum_x/__cnt_x），排序/导出/详情均以用户可见列为准
        full_headers = [h for h in (list(headers) if headers else list(rows[0].keys()))
                        if not _is_helper_col(h)]

        # 展示视图：来源列收敛/合一，业务列完整；排序与导出仍用全量列
        # 已合并的全局聚合结果无来源列，直接透传
        if merged:
            disp_headers, disp_rows, note = full_headers, rows, None
        else:
            disp_headers, disp_rows, note = display_view(full_headers, rows)
        self._render_result(disp_headers, disp_rows, note, merged=merged)

        while True:
            ans = Tui.shortcut('结果操作',
                               {'s': '排序', 'v': '行详情', 'e': '导出', 'q': '返回'},
                               default='q')
            if ans is None or ans == 'q':
                return
            if ans == 's':
                cols_line = ' · '.join(disp_headers)
                Tui.hint('可排序列：' +
                         TableRenderer.truncate(cols_line, max(20, Tui.term_width() - 14)))
                field = Tui.ask('排序列名', '')
                if field == '' or field == SENTINEL_EOF:
                    continue
                # 展示列 `table` 映射回原始 `table_name`（合并结果无此映射）
                sort_field = field if merged else ('table_name' if field == 'table' else field)
                desc = Tui.confirm('降序？', False)
                sorted_rows = TableRenderer.sort_rows(rows, sort_field, desc)
                if sorted_rows is None:
                    Tui._err(u"列 '%s' 不存在" % field)
                    continue
                rows = sorted_rows
                Tui.ok("已按 '%s' %s 排序" % (field, '降序' if desc else '升序'))
                if merged:
                    disp_headers, disp_rows, note = full_headers, rows, None
                else:
                    disp_headers, disp_rows, note = display_view(full_headers, rows)
                self._render_result(disp_headers, disp_rows, note, merged=merged)
                Tui._out('\n')
                continue
            if ans == 'v':
                self.view_row_detail(full_headers, rows)
                continue
            if ans == 'e':
                self.export_flow(full_headers, rows)

    def _render_result(self, disp_headers, disp_rows, note, merged=False):
        # 结果展示即一次「步骤切换」：清掉执行进度等噪音，只留结果表（清屏关闭时降级为留白）
        self.screen(u'结果')
        title = u'聚合结果' if merged else u'查询结果'
        Tui._out(u'  ' + Tui.bold(title) + u'  ' +
                 Tui.dim(u'%d 行 × %d 列' % (len(disp_rows), len(disp_headers))) + u'\n')
        Tui.rule()
        if note:
            Tui._out(u'  ' + Tui.dim(u'来源  %s' % note) + u'\n')
        self.renderer.render_paged(disp_headers, disp_rows)
        Tui._out('\n')

    def view_row_detail(self, full_headers, rows):
        """竖排查看某几行的完整字段（含来源列），解决横排表格截断看不到全长的问题。"""
        spec = Tui.ask('查看行号（如 2 或 2-5，回车取消）', '')
        if spec == '' or spec == SENTINEL_EOF:
            return
        spec = normalize_chars(spec.strip())
        a, b = None, None
        m = re.match(r'^(\d+)\s*-\s*(\d+)$', spec)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
        elif spec.isdigit():
            a = b = int(spec)
        total = len(rows)
        if a is None or a < 1 or b > total or a > b:
            Tui.warn('行号超出范围（共 %d 行）' % total)
            return
        if b - a > 10:
            Tui.warn('一次最多看 10 行详情')
            return
        self.renderer.render_vertical(full_headers, rows[a - 1:b], a - 1)
        Tui._out('\n')

    # ------------------------------------------------------------------
    # 导出（一次选格式 + 路径，回车用默认）
    # ------------------------------------------------------------------
    def export_flow(self, headers, rows):
        fmt_idx = Tui.menu('导出格式',
                           ['text  (.txt)', 'csv   (.csv)',
                            'markdown  (.md)', 'html  (.html)'], True, 10)
        if fmt_idx == -1:
            return
        ext_map = ['txt', 'csv', 'md', 'html']
        fmt_map = {0: 'text', 1: 'csv', 2: 'markdown', 3: 'html'}
        ext = ext_map[fmt_idx]

        default_file = self.rootDir + '/output/result_' + time.strftime('%Y%m%d_%H%M%S') + '.' + ext
        file_path = Tui.ask('导出路径', default_file)
        if file_path == SENTINEL_EOF:
            return
        if file_path != '' and file_path[0] != '/':
            file_path = self.rootDir + '/' + file_path

        norm_rows = TableRenderer.normalize_data(headers, rows)
        self.exporter.set_output_format(fmt_map[fmt_idx])
        self.exporter.set_max_rows(DEFAULT_MAX_ROWS)

        try:
            self.exporter.export(norm_rows, file_path, headers)
            Tui.ok('已导出 %d 行 → %s' % (self.exporter.get_current_row_count(), file_path))
        except RowLimitExceededError:
            Tui._err('行数超限已中断（已写入 %d 行）: %s'
                     % (self.exporter.get_current_row_count(), file_path))
        except Exception as e:
            Tui._err('导出失败: %s' % e)
