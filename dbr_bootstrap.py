# -*- coding: utf-8 -*-
from __future__ import unicode_literals
"""
dbr 引导（供脚本/测试复用）
将项目根加入 sys.path 并暴露常用类。
"""

import os
import sys

DBR_ROOT = os.path.dirname(os.path.abspath(__file__))
if DBR_ROOT not in sys.path:
    sys.path.insert(0, DBR_ROOT)

from core.config_loader import ConfigLoader  # noqa: E402
from core.sql_client import SqlClient, find_mysql_client  # noqa: E402
from core.sharding_router import ShardingRouter  # noqa: E402
from core.route_computation import RouteComputation  # noqa: E402
from core.query_executor import QueryExecutor  # noqa: E402
from core.data_aggregator import DataAggregator  # noqa: E402
from core.result_exporter import ResultExporter  # noqa: E402
from core.exceptions import (  # noqa: E402
    DbrError, QueryError, ExportError, RowLimitExceededError,
)
