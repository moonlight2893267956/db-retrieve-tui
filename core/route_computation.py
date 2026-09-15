# -*- coding: utf-8 -*-
from __future__ import unicode_literals
"""
业务值路由计算封装（对齐 PHP 版 core/RouteComputation.php）

给定业务值（如交易单号），结合规则，算出目标库表名。
"""

from .sharding_router import ShardingRouter
from .exceptions import DbrError


class RouteComputation(object):
    def __init__(self):
        self.router = ShardingRouter()

    def compute_target(self, value, rule):
        if rule.get('shardingType') != 'numeric':
            raise DbrError("业务值直查仅支持数字索引(numeric)模式规则")
        if not self.router.has_routing_field(rule):
            raise DbrError("该规则未配置 routingField，无法进行业务值精确定位")

        route = self.router.calculate_route(value, rule)
        db_prefix = rule.get('dbPrefix', '')
        table_prefix = rule.get('tablePrefix', '')
        db_name = db_prefix + route['dbIndex']
        table_name = table_prefix + route['dbIndex'] + '_' + route['tableIndex']
        return {
            'dbName': db_name,
            'tableName': table_name,
            'dbIndex': route['dbIndex'],
            'tableIndex': route['tableIndex'],
        }

    def can_route_field(self, rule):
        return (rule.get('shardingType') == 'numeric'
                and self.router.has_routing_field(rule))

    def get_routing_field(self, rule):
        return rule.get('routingField', '')
