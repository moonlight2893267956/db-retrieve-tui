# -*- coding: utf-8 -*-
from __future__ import unicode_literals
"""
CSV 解析器（对齐 PHP 版 core/CsvParser.php）

用于 tasks 批量查询场景：解析 CSV 表头与数据行，去除 BOM。
"""

import csv

from .exceptions import DbrError


class CsvParser(object):
    def parse(self, file_path):
        import os
        if not os.path.exists(file_path):
            raise DbrError("CSV文件不存在: %s" % file_path)

        try:
            fh = open(file_path, 'rb')
        except (IOError, OSError):
            raise DbrError("无法打开CSV文件: %s" % file_path)

        try:
            reader = csv.reader(fh)
            headers = None
            rows = []
            line_no = 0
            for raw in reader:
                line_no += 1
                if headers is None:
                    if not raw:
                        fh.close()
                        raise DbrError("CSV文件为空或格式错误: %s" % file_path)
                    headers = [_remove_bom(_dec(c).strip()) for c in raw]
                    continue
                if not raw or (len(raw) == 1 and raw[0].strip() == ''):
                    continue
                if len(raw) != len(headers):
                    continue
                rows.append([_dec(c) for c in raw])
        finally:
            fh.close()

        if not rows:
            raise DbrError("CSV文件没有数据行: %s" % file_path)

        return {'headers': headers, 'rows': rows}


def _remove_bom(s):
    if s.startswith(u'\ufeff'):
        return s[1:]
    if isinstance(s, bytes) and s[:3] == b'\xef\xbb\xbf':
        return s[3:]
    return s


def _dec(b):
    if isinstance(b, bytes):
        try:
            return b.decode('utf-8')
        except UnicodeDecodeError:
            return b.decode('gbk', 'replace')
    return b
