# -*- coding: utf-8 -*-
from __future__ import unicode_literals
"""
配置容器（对齐 PHP 版 core/ShardingConfig.php）

持有 databaseConfigs / shardingRules / queryTemplates / tasks。
"""

from .exceptions import DbrError


class ShardingConfig(object):
    _instance = None

    def __init__(self):
        self._sharding_rules = {}
        self._query_templates = {}
        self._database_configs = {}
        self._tasks = {}

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # -- 加载 ------------------------------------------------------------
    def load_config_dict(self, config):
        """从已解析的 dict 加载（由 ConfigLoader 负责读文件）。"""
        if not isinstance(config, dict):
            raise DbrError("配置格式错误：应为 dict")

        rules = config.get('shardingRules')
        if isinstance(rules, dict):
            self._sharding_rules.update(rules)

        tmpls = config.get('queryTemplates')
        if isinstance(tmpls, dict):
            self._query_templates.update(tmpls)

        dbs = config.get('databaseConfigs')
        if isinstance(dbs, dict):
            self._database_configs.update(dbs)

        tasks = config.get('tasks')
        if isinstance(tasks, dict):
            self._tasks.update(tasks)

        return True

    # -- 读取 ------------------------------------------------------------
    def get_sharding_rule(self, rule_name):
        if rule_name not in self._sharding_rules:
            raise DbrError("分库分表规则不存在: %s" % rule_name)
        return self._sharding_rules[rule_name]

    def get_query_template(self, template_name):
        if template_name not in self._query_templates:
            raise DbrError("查询模板不存在: %s" % template_name)
        return self._query_templates[template_name]

    def get_database_config(self, db_name):
        if db_name not in self._database_configs:
            raise DbrError("数据库配置不存在: %s" % db_name)
        return self._database_configs[db_name]

    # -- 写入 ------------------------------------------------------------
    def set_sharding_rule(self, rule_name, rule):
        self._sharding_rules[rule_name] = rule

    def set_query_template(self, template_name, template):
        self._query_templates[template_name] = template

    def set_database_config(self, db_name, config):
        self._database_configs[db_name] = config

    # -- 全量读取 --------------------------------------------------------
    def get_all_database_configs(self):
        return self._database_configs

    def get_all_sharding_rules(self):
        return self._sharding_rules

    def get_all_query_templates(self):
        return self._query_templates

    def get_all_tasks(self):
        return self._tasks

    def has_database_config(self, db_name):
        return db_name in self._database_configs

    # -- Task ------------------------------------------------------------
    def get_task(self, task_name):
        if task_name not in self._tasks:
            raise DbrError("Task配置不存在: %s" % task_name)
        task = self._tasks[task_name]
        self._validate_task(task)
        return task

    def _validate_task(self, task):
        if not task.get('name'):
            raise DbrError("Task配置缺少 name 字段")
        if not task.get('rule'):
            raise DbrError("Task配置缺少 rule 字段")
        if not task.get('dbName'):
            raise DbrError("Task配置缺少 dbName 字段")
        if not isinstance(task.get('outputFields'), list) or not task.get('outputFields'):
            raise DbrError("Task配置缺少 outputFields 字段或格式错误")
        if task['rule'] not in self._sharding_rules:
            raise DbrError("Task配置中的 rule '%s' 不存在" % task['rule'])
        rule = self._sharding_rules[task['rule']]
        if not rule.get('routingField'):
            raise DbrError("Task配置中的 rule '%s' 未配置 routingField" % task['rule'])
        if task['dbName'] not in self._database_configs:
            raise DbrError("Task配置中的 dbName '%s' 不存在" % task['dbName'])
