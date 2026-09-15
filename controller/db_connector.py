# -*- coding: utf-8 -*-
from __future__ import unicode_literals
"""
数据库连接器（对齐 PHP 版 controller/DbConnector.php）

负责：交互选择数据源、检查/引导密码文件、初始化连接配置。
"""

import os

from .tui import Tui
from core.config_loader import ConfigLoader
from core.password_reader import PasswordReader
from core.exceptions import DbrError, QueryError


class DbConnector(object):
    def __init__(self, loader, root_dir, sql_client):
        self.loader = loader
        self.rootDir = root_dir.rstrip('/')
        self.sql = sql_client
        self._initialized = {}

    # -- 密码 ------------------------------------------------------------
    def get_password_file(self, db_config):
        rel = db_config.get('passwordFile', '')
        if rel == '':
            return ''
        if rel[0] == '/':
            return rel
        return self.rootDir + '/' + rel

    def has_password_file(self, db_config):
        path = self.get_password_file(db_config)
        return path != '' and os.path.exists(path)

    def read_password(self, db_config):
        """读密码：password 明文优先，其次 passwordFile 首行。读不到返回 None。"""
        if db_config.get('password'):
            return db_config['password']
        path = self.get_password_file(db_config)
        if path == '' or not os.path.exists(path):
            return None
        return PasswordReader.instance().read_password(path)

    def has_password(self, db_config):
        return bool(db_config.get('password')) or self.has_password_file(db_config)

    def prompt_and_save_password(self, db_config):
        path = self.get_password_file(db_config)
        if path == '':
            return False
        d = os.path.dirname(path)
        if d and not os.path.isdir(d):
            try:
                os.makedirs(d, 0755)
            except OSError:
                Tui.err('无法创建密码目录: %s' % d)
                return False
        Tui.warn('该连接尚未配置密码文件: %s' % path)
        Tui.hint('将交互输入密码并保存为文件首行（保密，不回显）')
        pwd = Tui.ask_hidden('  请输入数据库密码（只读账号）: ')
        if not pwd:
            Tui.warn('未输入密码，取消')
            return False
        try:
            f = open(path, 'wb')
            f.write((pwd + '\n').encode('utf-8'))
            f.close()
        except (IOError, OSError):
            Tui.err('写入密码文件失败: %s' % path)
            return False
        try:
            os.chmod(path, 0600)
        except OSError:
            pass
        Tui._out(Tui.green('  密码已保存到: %s\n' % path))
        return True

    # -- 连接 ------------------------------------------------------------
    def ensure_connected(self, db_name):
        if db_name in self._initialized:
            return True
        if not self.loader.get_inner().has_database_config(db_name):
            raise DbrError('数据库连接配置不存在: %s' % db_name)
        db_config = self.loader.get_database_config(db_name)

        password = self.read_password(db_config)
        while password is None:
            if not Tui.confirm('该数据源未配置密码（可在配置写 password 明文，或提供密码文件），是否现在交互输入？', True):
                Tui.warn('已跳过连接 %s' % db_name)
                return False
            if not self.prompt_and_save_password(db_config):
                Tui.warn('跳过连接 %s' % db_name)
                return False
            password = self.read_password(db_config)

        self.sql.init_config(
            db_name,
            db_config.get('host'),
            db_config.get('port'),
            db_config.get('username'),
            password,
        )

        # 探活（连接可能耗时，加 loading 反馈；非 TTY 自动静默）
        try:
            with Tui.spinner(u'正在连接 %s…' % db_name):
                test = self.sql.query(db_name, 'SELECT 1 AS ok')
            if not test:
                Tui.err('连接失败: %s' % db_name)
                return False
        except QueryError as e:
            Tui.err('连接失败 (%s): %s' % (db_name, e))
            return False

        self._initialized[db_name] = True
        return True

    def select_and_connect(self):
        names = self.loader.get_database_names()
        if not names:
            Tui.err('没有任何数据源（alias 或 --config）')
            return False
        items = []
        for n in names:
            try:
                cfg = self.loader.get_database_config(n)
            except Exception:
                cfg = {}
            items.append(n + Tui.dim('   ' + ConfigLoader.describe_db(cfg)))

        Tui.header('dbr', 'MySQL 分库分表查询')
        idx = Tui.menu('选择数据源', items)
        if idx < 0:
            return False
        db_name = names[idx]

        if not self.ensure_connected(db_name):
            return False
        Tui.ok('已连接 %s' % db_name)
        return db_name
