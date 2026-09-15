# -*- coding: utf-8 -*-
from __future__ import unicode_literals
"""
跨分表聚合合并引擎

背景：整组遍历时每张表各自 GROUP BY，简单拼接 ≠ 全局汇总。
本模块把各分表结果按分组键合并为**真正的全局聚合**：

  COUNT/SUM -> 相加
  MAX/MIN   -> 取极值
  AVG       -> 按 SUM/COUNT 加权（子查询需追加隐藏列）

白名单之外（GROUP_CONCAT/STDDEV/COUNT(DISTINCT ...) 等）不可合并：
多表时明确拒绝并提示缩表，绝不猜测合并规则。

Python 2.7 兼容。
"""

import re

try:
    from collections import OrderedDict as _OrderedDict
except ImportError:  # pragma: no cover
    _OrderedDict = dict

from .sql_builder import split_top_level

# 可跨表合并的聚合函数白名单
MERGEABLE_FUNCS = ('COUNT', 'SUM', 'MAX', 'MIN', 'AVG')

# 任何聚合函数（用于识别「有聚合但不在白名单」）
_ANY_FUNC_RE = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(', re.UNICODE)
_AS_ALIAS_RE = re.compile(r'\s+AS\s+([A-Za-z_][A-Za-z0-9_]*)\s*$', re.IGNORECASE)
_TAIL_ALIAS_RE = re.compile(r'\s+([A-Za-z_][A-Za-z0-9_]*)\s*$')  # 'F_id DESC' / 'expr alias'


class MergeError(Exception):
    """无法安全合并（需缩表或改用单表查询）。"""


class AggColumn(object):
    """SELECT 列表中的一列解析结果。"""

    __slots__ = ('expr', 'alias', 'func', 'inner', 'is_agg', 'mergeable')

    def __init__(self, expr, alias, func, inner, is_agg, mergeable):
        self.expr = expr          # 表达式原文
        self.alias = alias        # 输出键名
        self.func = func          # 聚合函数名（大写）或 None
        self.inner = inner        # 聚合函数内部参数（原文）或 None
        self.is_agg = is_agg
        self.mergeable = mergeable


def parse_select_columns(fields):
    """解析 SELECT 列表，识别聚合列与其输出键名。"""
    cols = []
    for raw in split_top_level(fields):
        expr = raw.strip()
        alias = None
        m = _AS_ALIAS_RE.search(expr)
        if m:
            alias = m.group(1)
            expr_wo_alias = _AS_ALIAS_RE.sub(u'', expr).strip()
        else:
            expr_wo_alias = expr

        fm = _ANY_FUNC_RE.match(expr_wo_alias)
        func = None
        inner = None
        is_agg = False
        mergeable = False
        if fm:
            func = fm.group(1).upper()
            # 仅当形如 FUNC(...) 且括号内无嵌套函数、无 DISTINCT 时视为可解析的聚合
            close = expr_wo_alias.rfind(u')')
            inner = expr_wo_alias[fm.end():close].strip() if close > fm.end() else u''
            is_agg = True
            has_distinct = inner.upper().startswith(u'DISTINCT')
            mergeable = (func in MERGEABLE_FUNCS and u'(' not in inner
                         and not has_distinct)

        key = alias or _default_key(expr_wo_alias, alias)
        cols.append(AggColumn(expr_wo_alias, key, func, inner, is_agg, mergeable))
    return cols


def _default_key(expr, alias):
    """没有 AS 别名时的输出键名：MySQL 用表达式原文（去空格）作为列名。"""
    if alias:
        return alias
    return expr


def group_keys(group_by):
    """GROUP BY 的键列（原文，去 DESC/ASC 后缀）。"""
    keys = []
    for raw in split_top_level(group_by):
        k = _TAIL_ALIAS_RE.sub(u'', raw.strip())
        keys.append(k)
    return keys


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def analyze(parts):
    """
    分析是否可合并，返回 (cols, gkeys, need_avg_helpers)。
    Raises MergeError：含不可合并的聚合时（调用方据此拒绝多表）。
    """
    cols = parse_select_columns(parts.fields)
    aggs = [c for c in cols if c.is_agg]
    if not aggs:
        return cols, [], False
    bad = [c for c in aggs if not c.mergeable]
    if bad:
        names = u', '.join(sorted(set(c.func or u'?' for c in bad)))
        raise MergeError(
            u'聚合函数 %s 无法跨分表合并（仅支持 %s）；请缩到单表查询'
            % (names, u'/'.join(MERGEABLE_FUNCS)))
    if not parts.has_group:
        # 无 GROUP BY 的纯聚合（整表单行）：跨表无法确定合并语义
        raise MergeError(
            u'无 GROUP BY 的聚合无法跨分表合并（各表结果相互独立）；请缩到单表，'
            u'或加上 GROUP BY 分组键')
    gkeys = group_keys(parts.group_by)
    if not gkeys:
        raise MergeError(u'GROUP BY 无法解析，请检查分组字段')
    need_avg = any(c.func == u'AVG' for c in aggs)
    return cols, gkeys, need_avg


def _to_num(v):
    if v is None or v == '':
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _fmt_num(x):
    """整数则去掉 .0，保持展示直观。"""
    if x is None:
        return u''
    if abs(x - int(x)) < 1e-9:
        return u'%d' % int(x)
    return u'%s' % x


def merge(rows, parts):
    """
    把多分表结果合并为全局聚合。

    rows:  已 collect 的行列表（OrderedDict），含各分表原始列。
    parts: QueryParts

    返回合并后的行列表（OrderedDict）。行内含 table 来源列时会被丢弃。
    """
    cols, gkeys, _need_avg = analyze(parts)
    aggs = [c for c in cols if c.is_agg]
    plain = [c for c in cols if not c.is_agg]

    groups = _OrderedDict()
    order = []

    for row in rows:
        key = tuple(_cell(row, k) for k in gkeys)
        if key not in groups:
            groups[key] = {
                '_key': key,
                '_plain': {},
                '_agg': {},
                '_count': 0,
            }
            order.append(key)
            for c in plain:
                groups[key]['_plain'][c.alias] = _cell(row, c.expr, c.alias)
            for c in aggs:
                groups[key]['_agg'][c.alias] = None
        g = groups[key]
        g['_count'] += 1
        for c in aggs:
            cur = g['_agg'][c.alias]
            g['_agg'][c.alias] = _combine(c, cur, _cell(row, c.expr, c.alias), row)

    out = []
    for key in order:
        g = groups[key]
        nr = _OrderedDict()
        # 按原 SELECT 列表顺序输出，避免列序混乱；聚合列用合并值
        for c in cols:
            if c.is_agg:
                nr[c.alias] = _fmt_agg(c, g['_agg'][c.alias], g['_count'])
            else:
                # 分组键优先取行值，其次取键元组
                v = g['_plain'].get(c.alias, None)
                if v is None or v == u'':
                    v = g['_plain'].get(c.expr, None)
                if v is None or v == u'':
                    v = _key_val(g['_key'], gkeys, c.expr)
                nr[c.alias] = v if v is not None else u''
        out.append(nr)

    out = _apply_having(out, parts, aggs)
    out = _apply_order(out, parts)
    return out


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------
def _cell(row, expr, alias=None):
    """从行里取值：先按别名/原文，再按去掉表前缀的列名。"""
    for k in (alias, expr):
        if k and k in row:
            return row[k]
    # 表达式带表前缀 db.table.col -> col
    if expr and u'.' in expr:
        tail = expr.split(u'.')[-1]
        if tail in row:
            return row[tail]
    return u''


def _key_val(key, gkeys, k):
    try:
        return key[gkeys.index(k)]
    except ValueError:
        return u''


def _avg_helper_names(col):
    """AVG 的隐藏辅助列名（由 executor 在子查询追加）。"""
    base = _sanitize(col.inner) if col.inner else u'col'
    return u'__sum_%s' % base, u'__cnt_%s' % base


def _sanitize(expr):
    return re.sub(r'[^0-9A-Za-z_]', u'_', u'%s' % expr)


def _combine(col, cur, new, row=None):
    """按聚合函数把新值并入当前值。"""
    f = col.func
    if f == u'COUNT':
        a = _to_num(cur) or 0
        return a + (_to_num(new) or 0)
    if f == u'SUM':
        a = _to_num(cur)
        b = _to_num(new)
        if a is None and b is None:
            return None
        return (a or 0) + (b or 0)
    if f == u'MAX':
        return _extreme(cur, new, want_max=True)
    if f == u'MIN':
        return _extreme(cur, new, want_max=False)
    if f == u'AVG':
        sc, cc = _avg_helper_names(col)
        s = _to_num(row.get(sc)) if row is not None else None
        c = _to_num(row.get(cc)) if row is not None else None
        if s is None:
            v = _to_num(new)
            s, c = ((v or 0.0), (1 if v is not None else 0))
        acc = cur if isinstance(cur, tuple) else (0.0, 0)
        return (acc[0] + (s or 0.0), acc[1] + int(c or 0))
    return new


def _extreme(cur, new, want_max):
    a = cur
    b = new
    na, nb = _to_num(a), _to_num(b)
    if na is not None and nb is not None:
        a, b = na, nb
    if a is None or a == '':
        return b
    if b is None or b == '':
        return a
    try:
        if want_max:
            return b if b > a else a
        return b if b < a else a
    except TypeError:
        sa, sb = u'%s' % a, u'%s' % b
        return sb if ((sb > sa) if want_max else (sb < sa)) else sa


def avg_helper_select(parts):
    """
    若 SELECT 含 AVG，返回需要追加到子查询的隐藏列 SQL 片段列表：
      ['SUM(x) AS __sum_x', 'COUNT(x) AS __cnt_x', ...]
    以便跨表加权合并 AVG。其余情况返回 []。
    """
    cols = parse_select_columns(parts.fields)
    out = []
    for c in cols:
        if c.is_agg and c.func == u'AVG' and c.inner:
            sc, cc = _avg_helper_names(c)
            out.append(u'SUM(%s) AS %s' % (c.inner, sc))
            out.append(u'COUNT(%s) AS %s' % (c.inner, cc))
    return out


def _fmt_agg(col, val, nrows):
    if col.func == u'AVG':
        if isinstance(val, tuple):
            s, c = val
            return _fmt_num(s / c) if c else u''
        return _fmt_num(_to_num(val))
    if col.func in (u'COUNT', u'SUM', u'MAX', u'MIN'):
        n = _to_num(val)
        if n is not None:
            return _fmt_num(n)
        return val if val is not None else u''
    return val if val is not None else u''


def _apply_having(rows, parts, aggs):
    """HAVING 后置判定：支持简单比较 <col> <op> <number>。无法解析则原样返回。"""
    if not parts.has_having or not rows:
        return rows
    cond = parts.having.strip()
    m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*|\w+\s*\([^)]*\))\s*(>=|<=|<>|!=|=|>|<)\s*'
                 r'(-?\d+(?:\.\d+)?)\s*$', cond)
    if not m:
        # 复杂 HAVING：无法在客户端安全重算，交由调用方提示（此处不强判）
        return rows
    ref, op, num = m.group(1).strip(), m.group(2), float(m.group(3))
    key = ref
    # COUNT(*)/SUM(x) 形式映射到别名
    if ref not in rows[0]:
        for a in aggs:
            if a.func and a.alias and ref.upper().startswith(a.func):
                key = a.alias
                break
    def ok(r):
        v = _to_num(r.get(key))
        if v is None:
            return False
        if op == '>':
            return v > num
        if op == '<':
            return v < num
        if op in ('>=',):
            return v >= num
        if op in ('<=',):
            return v <= num
        if op in ('=',):
            return v == num
        return v != num
    return [r for r in rows if ok(r)]


def _apply_order(rows, parts):
    """合并后全局排序：解析 order_by 的 '列 [ASC|DESC]' 列表（多键、升降混合）。"""
    if not parts.has_order:
        return rows
    specs = []
    for raw in split_top_level(parts.order_by):
        item = raw.strip()
        desc = False
        m = re.match(r'^(.*?)\s+(DESC|ASC)$', item, re.IGNORECASE)
        if m:
            item = m.group(1).strip()
            desc = (m.group(2).upper() == 'DESC')
        specs.append((item, desc))
    if not specs:
        return rows

    def cmp_rows(a, b):
        for name, desc in specs:
            c = _cmp_cell(a.get(name, u''), b.get(name, u''))
            if c != 0:
                return -c if desc else c
        return 0

    import sys as _sys
    if _sys.version_info[0] == 2:
        return sorted(rows, cmp=cmp_rows)  # noqa: F821
    return sorted(rows, key=_functools_cmp_to_key(cmp_rows))


def _functools_cmp_to_key(cmp):
    import functools
    return functools.cmp_to_key(cmp)


def _cmp_cell(va, vb):
    na, nb = _to_num(va), _to_num(vb)
    if na is not None and nb is not None:
        if na == nb:
            return 0
        return -1 if na < nb else 1
    sa, sb = u'%s' % (va if va is not None else u''), u'%s' % (vb if vb is not None else u'')
    if sa == sb:
        return 0
    return -1 if sa < sb else 1

