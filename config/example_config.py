# -*- coding: utf-8 -*-
"""
dbr 可选配置示例（仅在需要时用 --config=config/example_config.py 指定）

默认情况下 dbr 直接使用当前 shell 的 mysql alias 作为数据源，无需任何配置文件。
本示例展示两件可选的事：

  1) shardingRules：为「智能缩表」提供路由字段，从而在 where 里给出路由值
     （如 F_user_id='1000000003'）时，只查唯一目标库表，避免全库全表遍历。
  2) databaseConfigs：自定义数据源（不依赖 alias，例如线上环境没有 alias 时）。

用法：
  python dbr.py --config=config/example_config.py
  # 与 alias 同时启用：alias 数据源 + 本配置的规则（同名数据源以 --config 为准）
"""

CONFIG = {
    # 可选：自定义数据源（若你的 shell 已有 mysql alias，无需在这里重复配置）
    'databaseConfigs': {
        # 'paybase': {
        #     'host': '10.0.0.10',
        #     'port': 3306,
        #     'username': 'db_user',
        #     'password': '...',            # 或 'passwordFile': '...'
        # },
    },

    # 可选：分库分表规则（仅服务「智能缩表」；向导主流程按 SHOW DATABASES/TABLES 展开）
    'shardingRules': {
        # 示例：demo_db_{db}.t_user_setting_{db}_{table}
        #       路由字段 F_user_id 末 3 位取前 2 位为库、末 1 位为表
        # 'paybase_user_setting': {
        #     'shardingType': 'numeric',
        #     'dbPrefix': 'demo_db_',
        #     'tablePrefix': 't_user_setting_',
        #     'routingField': 'F_user_id',
        #     'routingMethod': 'substring',
        #     'routingParams': {
        #         'dbIndexStart': -3, 'dbIndexLength': 2, 'tableIndexStart': -1,
        #     },
        # },
    },
}
