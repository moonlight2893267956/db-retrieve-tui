# -*- coding: utf-8 -*-
from __future__ import unicode_literals
"""
密码读取（对齐 PHP 版 core/PasswordReader.php）

约定：密码文件首行即密码，忽略空行。
"""


class PasswordReader(object):
    _instance = None

    def __init__(self):
        pass

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def read_password(self, filepath):
        """
        读取密码文件首行（忽略空行）。读不到返回 None。
        注意：返回真实密码，调用方不得打印。
        """
        import os
        if not os.path.exists(filepath):
            return None
        try:
            with open(filepath, 'r') as f:
                for line in f:
                    line = line.rstrip('\r\n')
                    if line == '':
                        continue
                    return line
        except (IOError, OSError):
            return None
        return None
