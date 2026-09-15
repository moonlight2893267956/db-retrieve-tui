# -*- coding: utf-8 -*-
from __future__ import unicode_literals
"""
数据聚合器（对齐 PHP 版 core/DataAggregator.php）

collect 会为每行补充分库分表标识字段（与 PHP 一致，不带下划线的 key 名）：
  * 数字索引模式：db_index / table_index
  * 日期分表模式：date
  * 两种模式都补：table_name
"""

try:
    from collections import OrderedDict as _OrderedDict
except ImportError:  # pragma: no cover
    _OrderedDict = dict

from .exceptions import DbrError


class DataAggregator(object):
    def aggregate(self, results, agg_type, field=''):
        if agg_type == 'sum':
            return self.sum(results, field)
        elif agg_type == 'count':
            return self.count(results)
        elif agg_type == 'avg':
            return self.avg(results, field)
        elif agg_type == 'collect':
            return self.collect(results)
        else:
            raise DbrError("不支持的聚合类型: %s" % agg_type)

    def sum(self, results, field):
        total = 0.0
        for r in results:
            if not r.success or not r.data:
                continue
            for row in r.data:
                if field in row:
                    total += _to_float(row[field])
        return total

    def count(self, results):
        total = 0
        for r in results:
            if not r.success:
                continue
            total += r.rowCount
        return total

    def avg(self, results, field):
        s = 0.0
        c = 0
        for r in results:
            if not r.success or not r.data:
                continue
            for row in r.data:
                if field in row:
                    s += _to_float(row[field])
                    c += 1
        return s / c if c > 0 else 0

    def collect(self, results):
        all_data = []
        for r in results:
            if not r.success or not r.data:
                continue
            for row in r.data:
                new_row = _OrderedDict(row)
                if r.dbIndex:
                    new_row['db_index'] = r.dbIndex
                    new_row['table_index'] = r.tableIndex
                else:
                    new_row['date'] = r.tableIndex
                new_row['table_name'] = r.tableName
                all_data.append(new_row)
        return all_data


def _to_float(v):
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0.0
