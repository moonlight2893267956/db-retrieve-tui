# -*- coding: utf-8 -*-
from __future__ import unicode_literals
"""
查询结果封装（对齐 PHP 版 core/QueryResult.php）
"""


class QueryResult(object):
    def __init__(self, db_index, table_index, table_name, data,
                 row_count, execution_time, success,
                 error='', table_exists=True):
        self.dbIndex = db_index
        self.tableIndex = table_index
        self.tableName = table_name
        self.data = data
        self.rowCount = row_count
        self.executionTime = execution_time
        self.success = success
        self.error = error
        self.tableExists = table_exists
