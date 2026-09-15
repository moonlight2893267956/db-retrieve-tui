# -*- coding: utf-8 -*-
from __future__ import unicode_literals
"""
分库分表路由引擎（对齐 PHP 版 core/ShardingRouter.php）

支持：
  * numeric 数字索引模式：生成所有 (db, table) 组合
  * date    日期分表模式：按 dateList / dateRange 生成
  * 智能路由：从 SQL 模板提取 routingField 等值 -> 精确定位单表
  * calculateRoute：substring / modulo 两种取值方式

Python 2.7 兼容。
"""

import re
from datetime import datetime, timedelta

from .exceptions import DbrError


class ShardingRouter(object):

    # -- 目标生成 --------------------------------------------------------
    def generate_targets(self, rule, template=None):
        """生成所有分库分表目标（支持智能路由）。"""
        if template is not None:
            try:
                intelligent = self.generate_intelligent_targets(rule, template)
                if intelligent is not None:
                    return intelligent
            except DbrError:
                raise
            except Exception:
                # 其他异常：回退全量查询
                pass
        return self.generate_all_targets(rule)

    def generate_all_targets(self, rule):
        stype = rule.get('shardingType')
        if not stype:
            raise DbrError("分库分表规则缺少 shardingType 字段")
        if stype == 'numeric':
            return self.generate_numeric_targets(rule)
        elif stype == 'date':
            return self.generate_date_targets(rule)
        else:
            raise DbrError("不支持的分库分表类型: %s" % stype)

    def generate_numeric_targets(self, rule):
        db_prefix = rule.get('dbPrefix', '')
        table_prefix = rule.get('tablePrefix', '')
        db_count = int(rule.get('dbCount', 1))
        table_count = int(rule.get('tableCount', 1))
        db_index_format = rule.get('dbIndexFormat', '0-99')
        table_index_format = rule.get('tableIndexFormat', '0-9')

        targets = []

        db_parts = db_index_format.split('-')
        db_start = int(db_parts[0])
        db_end = int(db_parts[1])
        db_padding = len(db_parts[0])

        tbl_parts = table_index_format.split('-')
        tbl_start = int(tbl_parts[0])
        tbl_end = int(tbl_parts[1])

        db_index = db_start
        while db_index <= db_end and db_index < db_count:
            db_index_str = str(db_index).rjust(db_padding, '0')
            db_name = db_prefix + db_index_str
            tbl_index = tbl_start
            while tbl_index <= tbl_end and tbl_index < table_count:
                table_name = table_prefix + db_index_str + '_' + str(tbl_index)
                targets.append({
                    'dbIndex': db_index_str,
                    'tableIndex': str(tbl_index),
                    'dbName': db_name,
                    'tableName': table_name,
                    'date': '',
                })
                tbl_index += 1
            db_index += 1
        return targets

    def generate_date_targets(self, rule):
        table_prefix = rule.get('tablePrefix', '')
        date_format = rule.get('dateFormat', 'Ymd')
        py_fmt = _php_date_format_to_python(date_format)

        date_list = rule.get('dateList')
        if isinstance(date_list, list) and date_list:
            dates = list(date_list)
        elif isinstance(rule.get('dateRange'), dict):
            start = rule['dateRange'].get('start', '')
            end = rule['dateRange'].get('end', '')
            dates = self.generate_date_range(start, end, py_fmt) if start and end else []
        else:
            raise DbrError("日期分表规则必须指定 dateList 或 dateRange")

        targets = []
        for date_str in dates:
            table_name = table_prefix + str(date_str)
            targets.append({
                'dbIndex': '',
                'tableIndex': str(date_str),
                'dbName': rule.get('dbPrefix') or '',
                'tableName': table_name,
                'date': str(date_str),
            })
        return targets

    def generate_date_range(self, start_date, end_date, fmt='%Y%m%d'):
        s = self._parse_date(start_date)
        if s is None:
            raise DbrError("无法解析开始日期: %s" % start_date)
        e = self._parse_date(end_date)
        if e is None:
            raise DbrError("无法解析结束日期: %s" % end_date)
        out = []
        cur = s
        while cur <= e:
            out.append(cur.strftime(fmt))
            cur = cur + timedelta(days=1)
        return out

    @staticmethod
    def _parse_date(date_str):
        for fmt in ('%Y-%m-%d', '%Y%m%d', '%Y/%m/%d', '%Y.%m.%d'):
            try:
                return datetime.strptime(date_str, fmt)
            except ValueError:
                continue
        return None

    # -- 路由计算 --------------------------------------------------------
    def calculate_route(self, value, rule):
        """根据业务值计算 (dbIndex, tableIndex)。仅 numeric 模式。"""
        if rule.get('shardingType') != 'numeric':
            raise DbrError("calculateRoute 仅支持数字索引模式")

        method = rule.get('routingMethod', 'modulo')
        params = rule.get('routingParams', {}) or {}

        if method == 'substring':
            db_start = int(params.get('dbIndexStart', -3))
            db_len = int(params.get('dbIndexLength', 2))
            tbl_start = int(params.get('tableIndexStart', -1))
            value = str(value)
            n = len(value)

            db_index_str = _py_substr(value, db_start, db_len)
            table_index_str = _py_substr(value, tbl_start, 1)
            return {'dbIndex': db_index_str, 'tableIndex': table_index_str}

        elif method == 'modulo':
            db_count = int(rule.get('dbCount', 100))
            table_count = int(rule.get('tableCount', 10))
            h = _crc32(str(value))
            db_index = abs(h) % db_count
            table_index = abs(h) % table_count
            db_index_format = rule.get('dbIndexFormat', '00-99')
            db_padding = len(db_index_format.split('-')[0])
            return {
                'dbIndex': str(db_index).rjust(db_padding, '0'),
                'tableIndex': str(table_index),
            }
        else:
            raise DbrError("不支持的路由方法: %s" % method)

    def get_all_targets(self, rule):
        return self.generate_targets(rule)

    # -- 智能路由 --------------------------------------------------------
    def extract_routing_value(self, sql_template, routing_field):
        """从 SQL 里提取 `field = 'value'` 的 value；未找到返回 None。"""
        pattern = r"\b%s\s*=\s*([\"'])([^\"']*)\1" % re.escape(routing_field)
        m = re.search(pattern, sql_template, re.IGNORECASE)
        if m:
            return m.group(2)
        return None

    def _should_use_intelligent_routing(self, rule, template):
        enable = template.get('enableIntelligentRouting', False)
        if enable is not True:
            return False
        if not rule.get('routingField'):
            raise DbrError("智能路由已启用，但分库分表规则中未配置 routingField")
        routing_value = self.extract_routing_value(
            template.get('sqlTemplate', ''), rule['routingField'])
        if routing_value is not None:
            return True
        raise DbrError(
            "智能路由已启用，但SQL模板中未找到路由字段 '%s' 的等值条件"
            % rule['routingField']
        )

    def generate_intelligent_targets(self, rule, template):
        if not self._should_use_intelligent_routing(rule, template):
            return None
        routing_value = self.extract_routing_value(
            template.get('sqlTemplate', ''), rule['routingField'])
        if routing_value is None:
            return None
        route = self.calculate_route(routing_value, rule)
        return [self._build_target_from_route(route, rule)]

    @staticmethod
    def _build_target_from_route(route, rule):
        db_index = route['dbIndex']
        table_index = route['tableIndex']
        db_prefix = rule.get('dbPrefix', '')
        table_prefix = rule.get('tablePrefix', '')
        return {
            'dbIndex': db_index,
            'tableIndex': table_index,
            'dbName': db_prefix + db_index,
            'tableName': table_prefix + db_index + '_' + table_index,
            'date': '',
        }

    def has_routing_field(self, rule):
        return bool(rule.get('routingField'))


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _py_substr(s, start, length):
    """
    模拟 PHP substr($s, $start, $length) 的负索引语义。
    start<0 表示从末尾算起；length>0 截取长度。
    """
    n = len(s)
    if start < 0:
        start = n + start
        if start < 0:
            start = 0
    if start >= n:
        return ''
    if length is None:
        return s[start:]
    if length < 0:
        end = n + length
        if end <= start:
            return ''
        return s[start:end]
    return s[start:start + length]


def _crc32(value):
    """返回与 PHP crc32 一致的 32 位无符号值（作为 int）。"""
    import binascii
    return binascii.crc32(value.encode('utf-8')) & 0xFFFFFFFF


def _php_date_format_to_python(fmt):
    """把常见 PHP 日期格式转 Python strftime（向导场景下主要为 Ymd）。"""
    mapping = {
        'Ymd': '%Y%m%d',
        'Y-m-d': '%Y-%m-%d',
        'Y/m/d': '%Y/%m/%d',
        'YmdHis': '%Y%m%d%H%M%S',
    }
    if fmt in mapping:
        return mapping[fmt]
    # 兜底：逐字符替换常见占位
    out = fmt.replace('Y', '%Y').replace('m', '%m').replace('d', '%d')
    return out
