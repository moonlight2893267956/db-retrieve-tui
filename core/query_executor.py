# -*- coding: utf-8 -*-
from __future__ import unicode_literals
"""
查询执行器（对齐 PHP 版 core/QueryExecutor.php）

职责：
  * executeQuery(target, template, rule)：单目标查询 + 重试 + 表存在检查
  * renderSqlTemplate：占位符替换
  * executeGuidedTargets：向导式批量查询（逐表执行同一 SQL，注入 where 占位符）
"""

import time

from .exceptions import DbrError, QueryError
from .query_result import QueryResult
from .sql_builder import QueryParts, build_sql
from .aggregate_merger import avg_helper_select


class QueryExecutor(object):
    def __init__(self, sql_client):
        self.sql = sql_client
        self.retryCount = 3
        self.timeout = 30

    def set_retry_count(self, count):
        self.retryCount = int(count)

    def set_timeout(self, seconds):
        self.timeout = int(seconds)

    # -- 单目标查询 ------------------------------------------------------
    def execute_query(self, target, template, rule):
        start = time.time()
        db_name = target.get('dbName', '')
        table_name = target.get('tableName', '')
        db_index = target.get('dbIndex', '')
        table_index = target.get('tableIndex', '')
        date = target.get('date', '')

        connection_name = template.get('dbName', '')
        if not connection_name:
            raise DbrError("查询模板缺少 dbName 字段（数据库连接配置名）")

        # 日期分表模式：检查表是否存在
        if rule.get('shardingType') == 'date':
            skip_missing = rule.get('skipMissingTables', True)
            if skip_missing and not self._check_table_exists(
                    db_name, table_name, connection_name):
                elapsed = time.time() - start
                return QueryResult(
                    db_index, table_index, table_name, [], 0, elapsed,
                    False, 'Table does not exist', False)

        sql = self.render_sql_template(template, target, rule)

        result = None
        error = u''
        attempt = 0
        while attempt < self.retryCount:
            try:
                # 连接时 `-D <目标库>`：兼容要求先选库的 MySQL 分支；SQL 仍写全 `db.table`
                result = self.sql.query_headers(connection_name, sql,
                                                timeout=self.timeout,
                                                database=db_name)
                break
            except QueryError as e:
                # 保留最后一次的真实错误；重试次数在下面补注
                error = u'%s' % e
                result = None
            attempt += 1
            if attempt < self.retryCount:
                time.sleep(1)

        if result is None and self.retryCount > 1 and error:
            error = u'%s（重试 %d 次）' % (error, self.retryCount)

        elapsed = time.time() - start

        if result is None:
            return QueryResult(db_index, table_index, table_name, [], 0,
                               elapsed, False, error, True)

        _headers, rows = result
        return QueryResult(db_index, table_index, table_name, rows,
                           len(rows), elapsed, True, '', True)

    def execute_batch(self, targets, template, rule):
        return [self.execute_query(t, template, rule) for t in targets]

    # -- 模板渲染 --------------------------------------------------------
    @staticmethod
    def render_sql_template(template, target, rule):
        sql = template.get('sqlTemplate', '')
        repl = {
            '{dbName}': target.get('dbName', ''),
            '{tableName}': target.get('tableName', ''),
            '{dbIndex}': target.get('dbIndex', ''),
            '{tableIndex}': target.get('tableIndex', ''),
            '{date}': target.get('date', ''),
        }
        for k, v in repl.items():
            sql = sql.replace(k, str(v))
        return sql

    def _check_table_exists(self, db_name, table_name, connection_name):
        # 用 `-D <db>` 连接后 SHOW TABLES LIKE：兼容要求先选库的 MySQL 分支
        sql = "SHOW TABLES LIKE '%s'" % table_name
        if not connection_name:
            raise DbrError("检查表存在性需要数据库连接配置名")
        try:
            rows = self.sql.query(connection_name, sql, timeout=self.timeout,
                                  database=(db_name.replace('`', '') or None))
        except QueryError:
            return False
        return bool(rows)

    # -- 向导式批量查询 --------------------------------------------------
    def execute_guided_targets(self, targets, parts, connection_name, quiet=False,
                               where_list=None):
        """
        对已解析的 (dbName, tableName) 目标集合逐表执行同一 SQL。
        parts: QueryParts（含 fields/where/group_by/having/order_by/limit）。
        where 等子句支持占位符 {db}/{table}/{dbIndex}/{tableIndex}。

        含 AVG 时会向子查询追加隐藏列 SUM(x)/COUNT(x)，供跨表加权合并。

        where_list: 可选。传入 list[str] 时表示「批量值清单查询」：
                    对每个目标表依次套用列表中的每个 where 片段各执行一次
                    （批次之间是 OR 语义，结果按顺序汇总）。
                    为 None 时行为与旧版完全一致（用 parts.where）。

        返回 {'results': [...], 'statistics': {...}}
        """
        if not isinstance(parts, QueryParts):
            # 兼容旧调用签名 (select_fields, where, limit)
            raise DbrError('execute_guided_targets 需要 QueryParts')

        if where_list is not None:
            return self._execute_with_where_list(
                targets, parts, connection_name, where_list, quiet=quiet)

        # AVG 辅助列（SUM/COUNT）仅在「多表跨分表合并」时需要：
        # 单表查询由 MySQL 直接算出 AVG，无需辅助列，否则会泄漏到结果展示。
        helper_cols = avg_helper_select(parts) if len(targets) > 1 else []
        # 多表 + GROUP BY：HAVING 里的聚合值是「分表局部值」，不能下推到子查询，
        # 否则会被错误过滤（如全局 c=224 但单表均 <200）。此处剥离 HAVING，
        # 交由合并后由 aggregate_merger 统一判定。
        per_table = parts
        if len(targets) > 1 and parts.has_group and parts.has_having:
            per_table = QueryParts(
                fields=parts.fields, where=parts.where, group_by=parts.group_by,
                having=u'', order_by=u'', limit=parts.limit)

        results = []
        statistics = {
            'total': len(targets),
            'success': 0,
            'failed': 0,
            'errors': [],
            'total_exec_time': 0.0,
        }

        total = len(targets)
        done = 0
        t0 = time.time()
        for target in targets:
            done += 1
            db_name = target.get('dbName', '')
            table_name = target.get('tableName', '')
            db_index = target.get('dbIndex', '')
            table_index = target.get('tableIndex', '')

            sql = build_sql(per_table, db_name, table_name, db_index, table_index)
            if helper_cols:
                # 追加隐藏辅助列：SELECT <fields>, SUM(x) AS __sum_x, COUNT(x) AS __cnt_x
                head, _, tail = sql.partition(u' FROM ')
                sql = u'%s, %s FROM %s' % (head, u', '.join(helper_cols), tail)

            template = {
                'dbName': connection_name,
                'sqlTemplate': sql,
                'aggregationType': 'collect',
            }
            rule = {'shardingType': 'numeric'}

            if not quiet and _progress_enabled():
                _progress_write(_progress_line(done, total, db_name, table_name,
                                               statistics, t0))

            res = self.execute_query(target, template, rule)
            results.append(res)
            statistics['total_exec_time'] += res.executionTime

            if res.success:
                statistics['success'] += 1
            else:
                statistics['failed'] += 1
                statistics['errors'].append({
                    'target': db_name + '.' + table_name,
                    'error': res.error,
                })

        if not quiet and _progress_enabled():
            _progress_write(u'\n')

        return {'results': results, 'statistics': statistics}

    # -- 批量值清单：目标表 × where 批次 ---------------------------------
    def _execute_with_where_list(self, targets, parts, connection_name,
                                 where_list, quiet=False):
        """
        批量值清单查询：对每个目标表依次套用 where_list 的每个片段执行。

        复用单目标 execute_query（同一套重试/超时/`-D` 选库），
        保证与普通查询链路一致。批次之间是 OR 语义，结果按顺序汇总（不去重）。
        """
        results = []
        statistics = {
            'total': 0,
            'success': 0,
            'failed': 0,
            'errors': [],
            'total_exec_time': 0.0,
        }

        if not where_list:
            raise DbrError('批量值清单查询需要至少一个 where 片段')

        total = len(targets) * len(where_list)
        statistics['total'] = total
        done = 0
        t0 = time.time()

        for target in targets:
            db_name = target.get('dbName', '')
            table_name = target.get('tableName', '')
            db_index = target.get('dbIndex', '')
            table_index = target.get('tableIndex', '')

            for where in where_list:
                done += 1
                one = QueryParts(
                    fields=parts.fields, where=where,
                    group_by=parts.group_by, having=parts.having,
                    order_by=parts.order_by, limit=parts.limit)
                sql = build_sql(one, db_name, table_name, db_index, table_index)
                template = {
                    'dbName': connection_name,
                    'sqlTemplate': sql,
                    'aggregationType': 'collect',
                }
                rule = {'shardingType': 'numeric'}

                if not quiet and _progress_enabled():
                    _progress_write(_progress_line(
                        done, total, db_name, table_name, statistics, t0))

                res = self.execute_query(target, template, rule)
                results.append(res)
                statistics['total_exec_time'] += res.executionTime

                if res.success:
                    statistics['success'] += 1
                else:
                    statistics['failed'] += 1
                    statistics['errors'].append({
                        'target': db_name + '.' + table_name,
                        'error': res.error,
                    })

        if not quiet and _progress_enabled():
            _progress_write(u'\n')

        return {'results': results, 'statistics': statistics}


def _progress_enabled():
    """仅在交互终端下刷新进度行；管道/重定向时保持输出干净（无 \\r 噪声）。"""
    try:
        import sys
        return bool(sys.stdout.isatty())
    except Exception:
        return False


def _progress_line(done, total, db_name, table_name, stat, t0):
    """原地刷新的遍历进度行：已耗时 + 成功/失败计数。"""
    elapsed = time.time() - t0
    tail = u'OK %d' % stat['success']
    if stat['failed']:
        tail += u' / FAIL %d' % stat['failed']
    return u'\r  [%d/%d] %s.%s  ·  %s  ·  %.1fs   ' % (
        done, total, db_name, table_name, tail, elapsed)


def _progress_write(s):
    """写进度到 stdout 并 flush（Python 2/3 兼容）。"""
    import sys
    sys.stdout.write(s)
    try:
        sys.stdout.flush()
    except Exception:
        pass
