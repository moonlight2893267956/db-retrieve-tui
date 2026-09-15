# -*- coding: utf-8 -*-
from __future__ import unicode_literals
"""
SQL 生成器（唯一的 SQL 拼接入口）

预览与真实执行都必须调用这里，避免「预览一个 SQL、执行另一个」的分歧。

  QueryParts        查询各子句的值对象
  build_sql()       按固定顺序拼出完整 SELECT
  split_top_level() 顶层逗号切分（括号感知，用于 SELECT 列表 / GROUP BY）

Python 2.7 兼容；不引入任何第三方依赖。
"""

try:
    from collections import OrderedDict as _OrderedDict
except ImportError:  # pragma: no cover
    _OrderedDict = dict


class QueryParts(object):
    """一次查询的各子句（均不含关键字本身）。"""

    __slots__ = ('fields', 'where', 'group_by', 'having', 'order_by', 'limit')

    def __init__(self, fields=u'*', where=u'', group_by=u'', having=u'',
                 order_by=u'', limit=u'100'):
        self.fields = fields if fields else u'*'
        self.where = where or u''
        self.group_by = group_by or u''
        self.having = having or u''
        self.order_by = order_by or u''
        self.limit = limit if limit is not None else u'100'

    # -- 便捷判断 --------------------------------------------------------
    @property
    def has_group(self):
        return self.group_by.strip() != u''

    @property
    def has_order(self):
        return self.order_by.strip() != u''

    @property
    def has_having(self):
        return self.having.strip() != u''

    def limit_sql(self):
        """返回 LIMIT 子句片段（不限则空串）。"""
        if self.limit == '' or unicode(self.limit).upper() == u'NONE':  # noqa: F821
            return u''
        return u'LIMIT %s' % unicode(self.limit)  # noqa: F821

    def to_dict(self):
        return _OrderedDict([
            ('fields', self.fields),
            ('where', self.where),
            ('group_by', self.group_by),
            ('having', self.having),
            ('order_by', self.order_by),
            ('limit', self.limit),
        ])

    def __repr__(self):
        return ('QueryParts(fields=%r, where=%r, group_by=%r, having=%r, '
                'order_by=%r, limit=%r)' % (
                    self.fields, self.where, self.group_by,
                    self.having, self.order_by, self.limit))


# ---------------------------------------------------------------------------
# 占位符
# ---------------------------------------------------------------------------
def apply_placeholders(text, db_index=u'', table_index=u''):
    """把 {db}/{table}/{dbIndex}/{tableIndex} 替换为目标的分库分表索引。"""
    if not text:
        return text
    for k, v in (('{db}', db_index), ('{table}', table_index),
                 ('{dbIndex}', db_index), ('{tableIndex}', table_index)):
        text = text.replace(k, u'%s' % v)
    return text


# ---------------------------------------------------------------------------
# 顶层逗号切分（括号感知）
# ---------------------------------------------------------------------------
def split_top_level(text):
    """
    按顶层逗号切分（忽略括号内的逗号），用于：
      SELECT 列表、GROUP BY 列表、ORDER BY 列表。

    例：'COUNT(DISTINCT a, b), c'  -> ['COUNT(DISTINCT a, b)', 'c']
    例：'SUM(IF(a, b, c))'         -> ['SUM(IF(a, b, c))']
    """
    if text is None:
        return []
    s = _to_text(text).strip()
    if s == '':
        return []
    out = []
    depth = 0
    quote = None       # 当前引号字符（' 或 " 或 `）
    buf = []
    for ch in s:
        if quote is not None:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in (u"'", u'"', u'`'):
            quote = ch
            buf.append(ch)
            continue
        if ch == u'(':
            depth += 1
            buf.append(ch)
            continue
        if ch == u')':
            if depth > 0:
                depth -= 1
            buf.append(ch)
            continue
        if ch == u',' and depth == 0:
            out.append(u''.join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    last = u''.join(buf).strip()
    if last != '':
        out.append(last)
    return [x for x in out if x != u'']


def _to_text(s):
    if isinstance(s, bytes):
        try:
            return s.decode('utf-8')
        except UnicodeDecodeError:
            return s.decode('gbk', 'replace')
    return s


# ---------------------------------------------------------------------------
# SQL 组装
# ---------------------------------------------------------------------------
def build_sql(parts, db_name, table_name, db_index=u'', table_index=u''):
    """
    按固定顺序拼出完整 SELECT：
      SELECT <fields> FROM db.table [WHERE][GROUP BY][HAVING][ORDER BY][LIMIT]

    占位符在 where/group_by/having/order_by 中一并替换。
    """
    d = apply_placeholders(db_name, db_index, table_index)
    t = apply_placeholders(table_name, db_index, table_index)
    sql = u'SELECT %s FROM %s.%s' % (parts.fields, d, t)

    if parts.where.strip() != u'':
        sql += u' WHERE ' + apply_placeholders(parts.where, db_index, table_index)
    if parts.group_by.strip() != u'':
        sql += u' GROUP BY ' + apply_placeholders(parts.group_by, db_index, table_index)
    if parts.having.strip() != u'':
        sql += u' HAVING ' + apply_placeholders(parts.having, db_index, table_index)
    if parts.order_by.strip() != u'':
        sql += u' ORDER BY ' + apply_placeholders(parts.order_by, db_index, table_index)

    lim = parts.limit_sql()
    if lim != u'':
        sql += u' ' + lim
    return sql


def merge_hint(parts, n_targets):
    """
    多表遍历且含聚合/分组时的合并提示（展示在预览区）。
    返回提示串或 None。
    """
    if n_targets <= 1:
        return None
    if not parts.has_group:
        return None
    return (u'将跨 %d 张表按分组键合并为全局聚合（COUNT/SUM 相加、'
            u'MAX/MIN 取极值、AVG 加权）；LIMIT 为每表上限，非全局。' % n_targets)
