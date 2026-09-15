# -*- coding: utf-8 -*-
from __future__ import unicode_literals
"""
值清单加载器（批量 IN 查询的输入层）

用于「拿一份值清单（如 log_id）批量查分表」场景，替代临时 PHP 脚本。
读取一个值文件，返回去重后的值列表：

  * 支持「无表头、每行一个值」的清单文件（对齐外部脚本的 data.csv）；
  * 也支持「首行为表头」的导出文件（自动识别，或由调用方强制指定）；
  * 支持指定列号（多列文件只取某一列）；
  * 去重并保持首次出现顺序；
  * BOM 去除、编码回退（UTF-8 -> GBK/GB18030）。

Python 2.7 兼容；不引入任何第三方依赖；不在此层做 SQL 转义（转义归 SQL 层）。
"""

import csv
import os
import re

from .exceptions import DbrError

try:
    from collections import OrderedDict as _OrderedDict
except ImportError:  # pragma: no cover
    _OrderedDict = dict


# 形如合法字段名：字母/下划线开头，仅含字母数字下划线
_FIELDNAME_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def _looks_like_field_name(value):
    """判断一个单元格是否「像字段名」：是合法标识符且不是纯数字。"""
    if value is None:
        return False
    s = value.strip()
    if s == '':
        return False
    if s.isdigit():
        return False
    return bool(_FIELDNAME_RE.match(s))


class ValueListLoader(object):
    def load(self, path, has_header=None, column=None, field=None):
        """
        读取值文件。

        参数:
            path:       文件路径
            has_header: None=自动识别；True=首行必为表头丢弃；False=无表头（不丢）
            column:     取第几列（0 基），对多列文件只取该列
            field:      可选。用于 IN 的字段名。当「未显式指定 column」且文件
                        「识别出表头」且表头中存在同名字段（大小写不敏感）时，
                        按字段名自动定位该列；找不到该字段则报错并列出可用列，
                        避免静默取错列。若显式给了 column，则 column 优先。

        返回:
            (values, meta)
              values: list，去重后的值（保持首次出现顺序）
              meta:   {'total_lines','skipped_empty','deduped',
                       'header_dropped','column','column_name'}
        """
        if not path:
            raise DbrError("未提供值文件路径")
        if not os.path.exists(path):
            raise DbrError("值文件不存在: %s" % path)
        if not os.path.isfile(path):
            raise DbrError("值文件不是普通文件: %s" % path)

        column_explicit = column is not None
        try:
            col = int(column)
        except (TypeError, ValueError):
            col = 0
        if col < 0:
            col = 0

        raw_rows, empty_lines = self._read_rows(path)

        meta = {
            'total_lines': len(raw_rows) + empty_lines,
            'skipped_empty': empty_lines,
            'deduped': 0,
            'header_dropped': False,
            'column': col,
            'column_name': u'',
        }

        if not raw_rows:
            raise DbrError("值文件没有数据行: %s" % path)

        # 判定首行是否表头
        header_cells = None
        drop_first = False
        if has_header is True:
            drop_first = True
            header_cells = [(_cell_to_text(c) or u'').strip() for c in raw_rows[0]]
        elif has_header is False:
            drop_first = False
        else:
            drop_first = self._detect_header(raw_rows, col)
            if drop_first:
                header_cells = [(_cell_to_text(c) or u'').strip() for c in raw_rows[0]]

        # 按字段名定位列（仅当未显式指定 column，且确有表头时）
        if field and not column_explicit and drop_first and header_cells:
            lowered = [h.lower() for h in header_cells]
            want = _cell_to_text(field)
            want = (want or u'').strip().lower()
            if want in lowered:
                col = lowered.index(want)
                meta['column'] = col
                meta['column_name'] = header_cells[col]
            else:
                avail = [h for h in header_cells if h != '']
                raise DbrError(
                    u"值文件表头中找不到字段 '%s'；可用列: %s\n"
                    u"  可用 --column=<N> 指定取第几列（0 基）"
                    % (field, u', '.join(avail) if avail else u'（无）'))

        start = 1 if drop_first else 0
        if drop_first:
            meta['header_dropped'] = True

        ordered = _OrderedDict()
        for row in raw_rows[start:]:
            val = self._cell(row, col)
            if val is None:
                meta['skipped_empty'] += 1
                continue
            v = val.strip()
            if v == '':
                meta['skipped_empty'] += 1
                continue
            if v not in ordered:
                ordered[v] = True

        values = list(ordered.keys())
        meta['deduped'] = len(values)
        if not values:
            raise DbrError("值文件没有读取到有效值: %s" % path)
        return values, meta

    # -- 内部 -------------------------------------------------------------
    @staticmethod
    def _read_rows(path):
        """读取为 (list[list[unicode]], 空行数)（BOM 去除、编码回退）。

        兼容 Python 2/3：Python 3 的 csv 需要文本模式，Python 2 需要二进制
        模式才能正确处理非 ASCII 字节。
        """
        import sys
        try:
            if sys.version_info[0] >= 3:
                fh = open(path, 'r', encoding='utf-8', errors='replace', newline='')
            else:
                fh = open(path, 'rb')
        except (IOError, OSError) as e:
            raise DbrError("无法打开值文件: %s (%s)" % (path, e))

        rows = []
        empty_lines = 0
        try:
            reader = csv.reader(fh)
            for raw in reader:
                if not raw:
                    empty_lines += 1
                    continue
                rows.append([_cell_to_text(_remove_bom(c)) for c in raw])
        finally:
            fh.close()
        return rows, empty_lines

    @staticmethod
    def _cell(row, col):
        if row is None:
            return None
        if col < len(row):
            return row[col]
        # 指定列不存在：单列文件兜底取首列
        if len(row) == 1:
            return row[0]
        return None

    @staticmethod
    def _detect_header(rows, col):
        """
        自动识别首行是否为表头。

        规则（保守，避免误丢真实值）：
          * 只有一行 -> 不丢（无法凭空判定）；
          * 首行该列「像字段名」（合法标识符且非纯数字），
            且第二行该列「不像字段名」-> 判为表头；
          * 其余情况不丢。
        """
        if len(rows) < 2:
            return False
        first = ValueListLoader._cell(rows[0], col)
        second = ValueListLoader._cell(rows[1], col)
        if first is None:
            return False
        if _looks_like_field_name(first) and not _looks_like_field_name(second):
            return True
        return False


def _remove_bom(s):
    if s.startswith(u'\ufeff'):
        return s[1:]
    if isinstance(s, bytes) and s[:3] == b'\xef\xbb\xbf':
        return s[3:]
    return s


def _cell_to_text(b):
    if isinstance(b, bytes):
        try:
            return b.decode('utf-8')
        except UnicodeDecodeError:
            try:
                return b.decode('gbk')
            except UnicodeDecodeError:
                return b.decode('gb18030', 'replace')
    return b


def validate_field_name(field):
    """校验字段名为合法标识符，非法则抛 DbrError（防 SQL 注入）。"""
    if field is None:
        raise DbrError("字段名不能为空")
    s = field.strip()
    if s == '':
        raise DbrError("字段名不能为空")
    if not _FIELDNAME_RE.match(s):
        raise DbrError("字段名非法（仅允许字母/数字/下划线，且不以数字开头）: %s" % field)
    return s
