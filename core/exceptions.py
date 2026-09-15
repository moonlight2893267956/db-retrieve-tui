# -*- coding: utf-8 -*-
from __future__ import unicode_literals
"""
dbr 异常体系（对齐 PHP 版 exceptions/）

  ShardingException            -> DbrError（基类）
    QueryException             -> QueryError
    ExportException            -> ExportError
      RowLimitExceededException -> RowLimitExceededError

Python 2.7 兼容。
"""


class DbrError(Exception):
    """分库分表/业务异常基类（等价 PHP ShardingException）。"""
    pass


class QueryError(DbrError):
    """查询执行相关异常（等价 PHP QueryException）。"""
    pass


class ExportError(DbrError):
    """结果导出相关异常（等价 PHP ExportException）。"""
    pass


class RowLimitExceededError(ExportError):
    """导出写入行数超过限制（等价 PHP RowLimitExceededException）。"""
    pass
