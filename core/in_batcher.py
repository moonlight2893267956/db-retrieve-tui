# -*- coding: utf-8 -*-
from __future__ import unicode_literals
"""
IN 分批器（批量值清单查询的核心）

把一份值清单切成若干批，每批生成一个
    <field> IN ('v1','v2',...)
形式的 WHERE 片段，供逐批查询后汇总。

要点：
  * 字段名做标识符校验（防 SQL 注入），值做 SQL 转义（' -> ''，\\ -> \\\\）；
  * 批大小默认 1000（对齐外部脚本 array_chunk 的常用值），非法回落默认；
  * 批次之间是 OR 语义（分多次查询后把结果汇总），与外部脚本行为一致。

Python 2.7 兼容；不引入任何第三方依赖。
"""

from .exceptions import DbrError
from .value_list_loader import validate_field_name

DEFAULT_BATCH_SIZE = 1000
MAX_BATCH_SIZE = 5000


class InBatcher(object):
    def __init__(self, batch_size=DEFAULT_BATCH_SIZE):
        self.batch_size = _normalize_batch_size(batch_size)

    # -- 分批 -------------------------------------------------------------
    def chunks(self, values):
        """把 values 按批大小切成 list[list[value]]（保持顺序）。"""
        size = self.batch_size
        out = []
        for i in range(0, len(values), size):
            out.append(list(values[i:i + size]))
        return out

    def batch_count(self, values):
        if not values:
            return 0
        return (len(values) + self.batch_size - 1) // self.batch_size

    # -- WHERE 片段 -------------------------------------------------------
    def build_in_where(self, field, values):
        """
        返回 list[str]，每个元素是一个完整 WHERE 片段：
            "F_log_id IN ('1','2','3')"

        field 非法或 values 为空时抛 DbrError。
        """
        safe_field = validate_field_name(field)
        if not values:
            raise DbrError("值清单为空，无法构造 %s IN (...)" % safe_field)

        out = []
        for batch in self.chunks(values):
            quoted = ','.join("'%s'" % escape_sql_literal(v) for v in batch)
            out.append('%s IN (%s)' % (safe_field, quoted))
        return out

    def preview_where(self, field, values, head=3, tail=2):
        """
        生成用于「SQL 预览」的压缩 WHERE 串（不打印上万个值）：
            F_log_id IN ('1','2','3',…,'998','999')  /* 共 1 批，每批 ≤1000 */
        """
        safe_field = validate_field_name(field)
        if not values:
            raise DbrError("值清单为空，无法构造 %s IN (...)" % safe_field)

        n = len(values)
        if n <= head + tail:
            shown = values
        else:
            shown = list(values[:head]) + [u'…'] + list(values[-tail:])
        parts = []
        for v in shown:
            if v == u'…':
                parts.append(u'…')
            else:
                parts.append(u"'%s'" % escape_sql_literal(v))
        note = u'  /* 共 %d 个值，%d 批，每批 ≤%d */' % (
            n, self.batch_count(values), self.batch_size)
        return u'%s IN (%s)%s' % (safe_field, u','.join(parts), note)


def _normalize_batch_size(batch_size):
    try:
        n = int(batch_size)
    except (TypeError, ValueError):
        return DEFAULT_BATCH_SIZE
    if n < 1:
        return DEFAULT_BATCH_SIZE
    if n > MAX_BATCH_SIZE:
        return MAX_BATCH_SIZE
    return n


def escape_sql_literal(value):
    """转义 SQL 字面量（与 batch_query_executor._escape_value 同规则）。"""
    if value is None:
        return ''
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
