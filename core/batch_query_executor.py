# -*- coding: utf-8 -*-
from __future__ import unicode_literals
"""
批量查询执行器（对齐 PHP 版 core/BatchQueryExecutor.php）

用于 tasks 场景：按 CSV 每行的路由字段精确路由到单表并查询。
"""

from .sharding_router import ShardingRouter
from .query_executor import QueryExecutor
from .exceptions import DbrError


class BatchQueryExecutor(object):
    def __init__(self, sql_client):
        self.sql = sql_client

    def validate_routing_field(self, headers, rule):
        if not rule.get('routingField'):
            raise DbrError("分库分表规则未配置 routingField")
        routing_field = rule['routingField']
        lower_headers = [h.lower() for h in headers]
        if routing_field.lower() not in lower_headers:
            raise DbrError(
                "CSV表头必须包含路由字段 '%s'，当前表头: %s"
                % (routing_field, ', '.join(headers))
            )

    def _generate_sql(self, row, headers, task):
        output_fields = task.get('outputFields') or []
        if not output_fields:
            raise DbrError("Task配置缺少 outputFields 字段")
        select_clause = 'SELECT ' + ','.join(output_fields)
        from_clause = ' FROM {dbName}.{tableName}'

        where_conditions = []
        for i in range(len(headers)):
            field = headers[i]
            value = row[i]
            if value is None or value == '':
                continue
            where_conditions.append("%s = '%s'" % (field, _escape_value(value)))
        if not where_conditions:
            raise DbrError("CSV行数据为空，无法生成WHERE条件")
        return select_clause + from_clause + ' WHERE ' + ' AND '.join(where_conditions)

    def execute_batch(self, csv_rows, csv_headers, task, rule):
        results = []
        statistics = {
            'total': len(csv_rows),
            'success': 0,
            'failed': 0,
            'skipped': 0,
            'errors': [],
        }

        router = ShardingRouter()
        executor = QueryExecutor(self.sql)
        executor.set_retry_count(3)
        executor.set_timeout(30)

        routing_field = rule['routingField']
        routing_field_index = -1
        for i, h in enumerate(csv_headers):
            if h.lower() == routing_field.lower():
                routing_field_index = i
                break
        if routing_field_index == -1:
            raise DbrError("无法找到路由字段 '%s' 在CSV中的位置" % routing_field)

        for row_index, row in enumerate(csv_rows):
            csv_row_number = row_index + 2
            progress = "%d/%d" % (row_index + 1, statistics['total'])
            print "[%s] 查询第%d行数据 ... " % (progress, csv_row_number),

            try:
                if routing_field_index >= len(row) or row[routing_field_index] == '':
                    statistics['skipped'] += 1
                    statistics['errors'].append({'row': csv_row_number, 'error': '路由字段值缺失'})
                    print "跳过（路由字段值缺失）"
                    continue

                sql_template = self._generate_sql(row, csv_headers, task)
                template = {
                    'sqlTemplate': sql_template,
                    'dbName': task['dbName'],
                    'enableIntelligentRouting': True,
                }
                targets = router.generate_intelligent_targets(rule, template)
                if not targets:
                    statistics['skipped'] += 1
                    statistics['errors'].append({'row': csv_row_number, 'error': '无法使用智能路由'})
                    print "跳过（无法使用智能路由）"
                    continue

                target = targets[0]
                result = executor.execute_query(target, template, rule)
                if result.success:
                    statistics['success'] += 1
                    for data_row in result.data:
                        data_row['csv_row_number'] = csv_row_number
                        results.append(data_row)
                    print "成功 (行数: %d, 耗时: %.3fs)" % (result.rowCount, result.executionTime)
                else:
                    statistics['failed'] += 1
                    statistics['errors'].append({'row': csv_row_number, 'error': result.error})
                    print "失败 (%s)" % result.error
            except Exception as e:
                statistics['failed'] += 1
                statistics['errors'].append({'row': csv_row_number, 'error': u'%s' % e})
                print "失败 (%s)" % e

        return {'results': results, 'statistics': statistics}


def _escape_value(value):
    s = str(value)
    s = s.replace("'", "''")
    s = s.replace("\\", "\\\\")
    return s
